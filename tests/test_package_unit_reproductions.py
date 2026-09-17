"""Producer-side controls for the bounded S2 correction: source row binding and brief containment."""

from __future__ import annotations

import copy
import errno
import hashlib
import html
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import pytest

import test_data_access_contract as authority
import test_package_role_identity as s2
from test_data_access_contract import _desktop_fixture, _root_fixture  # noqa: F401  # existing pytest fixtures
from test_package_unit_gates import DS_LUID, DS_UNIT, UNIT, _brief, _bundle, _write_input_manifest, _write_receipt, pkg


def selected_input(root: Path) -> tuple[Path, Path, dict[str, Any]]:
    """One independently declared datasource row and its source bytes."""
    bundle = root / "bundle"
    bundle.mkdir(parents=True)
    asset = root / "assets" / f"{DS_LUID}_{DS_UNIT}.tds"
    asset.parent.mkdir()
    asset.write_text("<datasource/>", encoding="utf-8")
    row = {
        "name": DS_UNIT,
        "staged_input_path": str(asset),
        "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
    }
    write_rows(bundle, [row])
    return bundle, asset, row


def write_rows(bundle: Path, rows: Any) -> None:
    """Write the source declaration, not a package produced by the implementation under test."""
    (bundle / "input_manifest.json").write_text(json.dumps({"assets": rows}), encoding="utf-8")


def test_datasource_selection_returns_the_unique_row_and_walked_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, asset, row = selected_input(tmp_path)
    walked = {}
    original = pkg.pfs.walk_package

    def tracked(root: Path):
        files, findings, empty = original(root)
        walked.update(files)
        return files, findings, empty

    monkeypatch.setattr(pkg.pfs, "walk_package", tracked)
    selected = pkg.resolve_asset(bundle, DS_UNIT, {}, asset.parent)

    assert selected.row == row, "resolution must retain the selecting row, not merely its route"
    assert selected.path is walked[asset.name]
    assert selected.route == "input_manifest.staged_input_path"
    pkg.assert_declared_digest(DS_UNIT, selected)


@pytest.mark.parametrize("logical_name", [False, True], ids=["filename-name", "logical-name"])
def test_staged_input_path_cannot_change_the_selecting_rows_basename(tmp_path: Path, logical_name: bool) -> None:
    bundle, asset, row = selected_input(tmp_path)
    if not logical_name:
        row["name"] = asset.name
    other = asset.with_name("Other.tds")
    other.write_text("<different/>", encoding="utf-8")
    row["staged_input_path"] = str(other)
    write_rows(bundle, [row])

    with pytest.raises(pkg.PackagingError, match="^input_manifest_path_mismatch$"):
        pkg.resolve_asset(bundle, DS_UNIT, {}, asset.parent)


def test_selected_digest_is_checked_without_a_second_manifest_lookup(tmp_path: Path) -> None:
    bundle, asset, row = selected_input(tmp_path)
    asset.write_text("<different/>", encoding="utf-8")
    selected = pkg.resolve_asset(bundle, DS_UNIT, {}, asset.parent)
    row["sha256"] = hashlib.sha256(asset.read_bytes()).hexdigest()
    write_rows(bundle, [row])

    with pytest.raises(pkg.PackagingError, match="^input_manifest_digest_mismatch$"):
        pkg.assert_declared_digest(DS_UNIT, selected)


@pytest.mark.parametrize("distinct", [False, True], ids=["duplicate-row", "two-prefixes"])
def test_datasource_rows_never_choose_the_first_match(tmp_path: Path, distinct: bool) -> None:
    bundle, asset, row = selected_input(tmp_path)
    second = dict(row)
    if distinct:
        second["name"] = f"99999999-2222-3333-4444-555555555555_{DS_UNIT}.tds"
        copied = asset.with_name(second["name"])
        shutil.copyfile(asset, copied)
        second["staged_input_path"] = str(copied)
    write_rows(bundle, [row, second])

    for handover in ({}, {"workbook": {"source_id": asset.name}}):
        with pytest.raises(pkg.PackagingError, match="^source_asset_row_ambiguous$"):
            pkg.resolve_asset(bundle, DS_UNIT, handover, asset.parent)


def test_datasource_candidates_never_choose_the_first_directory(tmp_path: Path) -> None:
    bundle, asset, _row = selected_input(tmp_path)
    second = bundle / "assets" / asset.name
    second.parent.mkdir()
    shutil.copyfile(asset, second)

    with pytest.raises(pkg.PackagingError, match="^source_asset_candidate_ambiguous$"):
        pkg.resolve_asset(bundle, DS_UNIT, {}, asset.parent)


@pytest.mark.parametrize("locator", ["absolute", "basename", "other-separator"])
def test_source_leaf_selects_the_logical_row_without_using_redaction_as_identity(tmp_path: Path, locator: str) -> None:
    bundle, asset, row = selected_input(tmp_path)
    source_id = str(asset) if locator == "absolute" else asset.name
    if locator == "other-separator":
        separator = "/" if os.name == "nt" else "\\"
        source_id = separator.join(("staged", "assets", asset.name))
    raw = {"workbook": {"source_id": source_id}}
    shipped, redactions = pkg.scope_handover(raw, DS_UNIT)

    selected = pkg.resolve_asset(bundle, DS_UNIT, raw, asset.parent)
    assert selected.path == asset and selected.row == row
    assert selected.route == "handover.workbook.source_id"
    pkg.assert_declared_digest(DS_UNIT, selected)
    assert raw["workbook"]["source_id"] == source_id
    if locator == "absolute":
        assert redactions and shipped["workbook"]["source_id"] != source_id
        with pytest.raises(pkg.PackagingError, match="^input_manifest_name_invalid$"):
            pkg.resolve_asset(bundle, DS_UNIT, shipped, asset.parent)
    else:
        assert not redactions
        assert pkg.resolve_asset(bundle, DS_UNIT, shipped, asset.parent) == selected


@pytest.mark.parametrize("handover_present", [False, True])
def test_legacy_filename_only_row_keeps_its_digest_and_exact_source(tmp_path: Path, handover_present: bool) -> None:
    bundle, asset, row = selected_input(tmp_path)
    row["name"] = asset.name
    del row["staged_input_path"]
    write_rows(bundle, [row])
    handover = {"workbook": {"source_id": asset.name}} if handover_present else {}

    selected = pkg.resolve_asset(bundle, DS_UNIT, handover, asset.parent)
    assert selected.path == asset and selected.row == row
    pkg.assert_declared_digest(DS_UNIT, selected)


@pytest.mark.parametrize("staged", [None, True, [], {}, "", " ", "<redacted: host path>"])
def test_present_invalid_staged_path_never_falls_back_to_the_filename(tmp_path: Path, staged: Any) -> None:
    bundle, asset, row = selected_input(tmp_path)
    row["name"] = asset.name
    row["staged_input_path"] = staged
    write_rows(bundle, [row])

    with pytest.raises(pkg.PackagingError, match="^input_manifest_path_invalid$"):
        pkg.resolve_asset(bundle, DS_UNIT, {"workbook": {"source_id": asset.name}}, asset.parent)


@pytest.mark.parametrize("source_id", [None, True, [], "", " ", "<redacted: host path>"])
def test_present_invalid_handover_identity_never_falls_back_to_the_row(tmp_path: Path, source_id: Any) -> None:
    bundle, asset, _row = selected_input(tmp_path)
    with pytest.raises(pkg.PackagingError, match="^input_manifest_name_invalid$"):
        pkg.resolve_asset(bundle, DS_UNIT, {"workbook": {"source_id": source_id}}, asset.parent)


@pytest.mark.parametrize("fault", ["row-unit", "source-unit", "source-prefix", "source-case", "arbitrary-prefix"])
def test_handover_physical_leaf_cannot_bypass_logical_or_staged_identity(tmp_path: Path, fault: str) -> None:
    bundle, asset, row = selected_input(tmp_path)
    source_id = asset.name
    if fault == "row-unit":
        row["name"] = "Other"
    elif fault == "source-unit":
        source_id = "Other.tds"
    elif fault == "source-prefix":
        source_id = f"99999999-2222-3333-4444-555555555555_{DS_UNIT}.tds"
    elif fault == "source-case":
        source_id = asset.name.lower()
    else:
        source_id = f"export_{DS_UNIT}.tds"
    write_rows(bundle, [row])

    with pytest.raises(pkg.PackagingError, match="^input_manifest_(name|path)_mismatch$"):
        pkg.resolve_asset(bundle, DS_UNIT, {"workbook": {"source_id": source_id}}, asset.parent)


@pytest.mark.parametrize("handover_present", [False, True])
def test_foreign_staged_path_cannot_be_rescued_by_a_local_same_named_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handover_present: bool
) -> None:
    bundle, asset, row = selected_input(tmp_path)
    row["staged_input_path"] = (
        f"/foreign/assets/{asset.name}" if os.name == "nt" else f"C:\\foreign\\assets\\{asset.name}"
    )
    write_rows(bundle, [row])
    assert not pkg.is_host_native(row["staged_input_path"])

    def must_not_walk(_base: Path) -> None:
        pytest.fail("a foreign staged locator cannot authorize fallback source bytes")

    monkeypatch.setattr(pkg.pfs, "walk_package", must_not_walk)
    handover = {"workbook": {"source_id": asset.name}} if handover_present else {}
    selected = pkg.resolve_asset(bundle, DS_UNIT, handover, asset.parent)
    assert selected.path is None and selected.route == "unresolved" and selected.row == row


@pytest.mark.parametrize("digest", [True, [], {}, "", "not-a-digest"])
def test_selected_rows_bad_digest_types_refuse(tmp_path: Path, digest: Any) -> None:
    bundle, asset, row = selected_input(tmp_path)
    row["sha256"] = digest
    write_rows(bundle, [row])
    selected = pkg.resolve_asset(bundle, DS_UNIT, {}, asset.parent)

    with pytest.raises(pkg.PackagingError, match="^input_manifest_digest_invalid$"):
        pkg.assert_declared_digest(DS_UNIT, selected)


@pytest.mark.parametrize("rows", [True, {}, [None], [{"name": True}]])
def test_invalid_input_rows_do_not_disappear(tmp_path: Path, rows: Any) -> None:
    bundle, asset, _row = selected_input(tmp_path)
    write_rows(bundle, rows)

    with pytest.raises(pkg.UnassessableInput) as caught:
        pkg.resolve_asset(bundle, DS_UNIT, {}, asset.parent)
    assert caught.value.reasons == ["input_manifest_row_invalid"]


@pytest.mark.parametrize("tail", ['"extra": 0, "extra": 1', '"extra": NaN', '"extra": 1e999'])
def test_input_manifest_uses_the_existing_strict_json_parser(tmp_path: Path, tail: str) -> None:
    bundle, asset, _row = selected_input(tmp_path)
    path = bundle / "input_manifest.json"
    path.write_text(path.read_text(encoding="utf-8")[:-1] + "," + tail + "}", encoding="utf-8")

    with pytest.raises(pkg.UnassessableInput) as caught:
        pkg.resolve_asset(bundle, DS_UNIT, {}, asset.parent)
    assert caught.value.reasons == ["input_manifest_invalid"]


@pytest.mark.parametrize("report_mode", ["none", "external", "under-output"])
def test_single_brief_is_refused_for_a_multi_unit_command_before_writing(  # pylint: disable=too-many-locals
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], report_mode: str
) -> None:
    bundle, _oracle, _objects = _bundle(tmp_path, covered=None, datasource_only=True)
    brief = _brief(tmp_path, UNIT)
    original = brief.read_bytes()
    out = tmp_path / "packages"
    report = out / "reporting" / "status.json" if report_mode == "under-output" else tmp_path / "status.json"
    stale = b'{"stale_success": true}\n'
    if report_mode == "external":
        report.write_bytes(stale)
    command = ["--bundle", str(bundle), "--out", str(out), "--brief", str(brief), "--quiet", "--assemble-only"]
    if report_mode != "none":
        command.extend(["--json", str(report)])
    replaced = []
    replace = Path.replace

    def observed_replace(path: Path, target: Path) -> Path:
        assert target == report, "no package may be published while refusing a shared brief"
        assert path != target and path.parent == target.parent
        payload = json.loads(path.read_bytes())
        assert payload["requested"] == sorted([UNIT, DS_UNIT])
        assert payload["construction"]["totals"] == {"requested": 2, "assembled": 0, "blocked": 2}
        if report_mode == "external":
            assert target.read_bytes() == stale, "the prior report must remain intact until complete replacement"
        replaced.append(path)
        return replace(path, target)

    def must_not_construct(*_args: object, **_kwargs: object) -> None:
        pytest.fail("one shared brief must refuse before any package construction")

    monkeypatch.setattr(pkg, "package_unit", must_not_construct)
    monkeypatch.setattr(pkg, "_prepare_out", must_not_construct)
    monkeypatch.setattr(Path, "replace", observed_replace)
    assert pkg.main(command) == 5
    captured = capsys.readouterr()
    assert captured.out.strip() == pkg.NOT_START_READY_NOTICE
    assert captured.err.count("brief_requires_one_unit") == 2
    assert brief.read_bytes() == original, "the caller's brief is read-only"
    if report_mode != "none":
        payload = json.loads(report.read_bytes())
        assert payload["requested"] == sorted([UNIT, DS_UNIT])
        assert payload["units"] == payload["refused"] == payload["unaccounted"] == []
        assert [row["unit"] for row in payload["failed"]] == sorted([UNIT, DS_UNIT])
        assert [row["reason_code"] for row in payload["failed"]] == ["brief_requires_one_unit"] * 2
        assert [row["unit"] for row in payload["construction"]["blocked"]] == sorted([UNIT, DS_UNIT])
        assert all(row["status"] == "BLOCKED" for row in payload["construction"]["blocked"])
        assert payload["construction"]["totals"] == {"requested": 2, "assembled": 0, "blocked": 2}
        assert payload["dispatch_readiness"]["status"] == "NOT_EVALUATED"
        assert "stale_success" not in payload
        assert len(replaced) == 1 and not replaced[0].exists()
    else:
        assert not replaced and not report.exists()
    if report_mode == "under-output":
        assert {path.relative_to(out).as_posix() for path in out.rglob("*")} == {"reporting", "reporting/status.json"}
    else:
        assert not out.exists(), "a single typed brief must not be broadcast into any package"


@pytest.mark.parametrize("variant", ["shared", "wrong-unit", "wrong-scope", "unsafe-text", "missing"])
def test_brief_refusal_preserves_existing_package_bytes_even_with_discard_edits(tmp_path: Path, variant: str) -> None:
    """Edit-discard permission does not authorize writes after a supplied-brief refusal."""
    bundle, oracle, _objects = _bundle(tmp_path, covered=None, datasource_only=True)
    out = tmp_path / "packages"
    brief = _brief(tmp_path, UNIT)
    pkg.package_unit(bundle, UNIT, out, oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=brief)
    edited = out / UNIT / "operator-edit.txt"
    edited.write_bytes(b"work already invested in the existing package")
    original = {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()}
    original_dirs = {path.relative_to(out) for path in out.rglob("*") if path.is_dir()}
    if variant == "wrong-unit":
        brief = _brief(tmp_path, "OtherUnit")
    elif variant == "wrong-scope":
        brief = _brief(tmp_path, UNIT, "model_only")
    elif variant == "unsafe-text":
        brief.write_bytes(brief.read_bytes() + b"\nX-Tableau-Auth: PRIVATE_BRIEF_CANARY\n")
    elif variant == "missing":
        brief = tmp_path / "absent-brief.md"
    original_brief = brief.read_bytes() if brief.is_file() else None
    report = tmp_path / "status.json"
    report.write_text('{"stale_success": true}', encoding="utf-8")
    command = [
        "--bundle",
        str(bundle),
        "--out",
        str(out),
        "--brief",
        str(brief),
        "--json",
        str(report),
        "--discard-package-edits",
        "--quiet",
    ]
    if variant != "shared":
        command.extend(["--unit", UNIT])

    assert pkg.main(command) == 5
    payload = json.loads(report.read_bytes())
    assert "stale_success" not in payload
    assert payload["construction"]["totals"] == {
        "requested": 2 if variant == "shared" else 1,
        "assembled": 0,
        "blocked": 2 if variant == "shared" else 1,
    }
    assert {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()} == original
    assert {path.relative_to(out) for path in out.rglob("*") if path.is_dir()} == original_dirs
    assert (brief.read_bytes() if brief.is_file() else None) == original_brief


@pytest.mark.parametrize("placement", ["absent-selected", "selected", "other", "nested-other"])
@pytest.mark.parametrize("filename", ["package-manifest.json", "handover.md"])
def test_json_destination_cannot_enter_real_or_prospective_packages(  # pylint: disable=too-many-locals
    tmp_path: Path, placement: str, filename: str
) -> None:
    """The production manifest and every neighboring artifact stay byte-identical after misuse."""
    bundle, oracle, _objects = _bundle(tmp_path, covered=None, datasource_only=True)
    out = tmp_path / "packages"
    brief = _brief(tmp_path, UNIT)
    package_parent = out / "batch" if placement == "nested-other" else out
    if placement != "absent-selected":
        pkg.package_unit(
            bundle, UNIT, package_parent, oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=brief
        )
    report = package_parent / UNIT / filename
    selected = DS_UNIT if placement in ("other", "nested-other") else UNIT
    original_brief = brief.read_bytes()
    original_files = {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()}
    original_dirs = {path.relative_to(out) for path in out.rglob("*") if path.is_dir()}
    with pytest.raises(SystemExit) as refused:
        pkg.main(
            [
                "--bundle",
                str(bundle),
                "--out",
                str(out),
                "--unit",
                selected,
                "--brief",
                str(brief if selected == DS_UNIT else tmp_path / "missing-brief.md"),
                "--json",
                str(report),
                "--discard-package-edits",
            ]
        )
    assert refused.value.code == 2
    assert brief.read_bytes() == original_brief
    assert {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()} == original_files
    assert {path.relative_to(out) for path in out.rglob("*") if path.is_dir()} == original_dirs
    if placement == "absent-selected":
        assert not out.exists(), "misuse must not manufacture a discoverable package marker"


@pytest.mark.parametrize("nested", [False, True], ids=["flat", "nested"])
@pytest.mark.parametrize("nonempty", [False, True], ids=["empty-marker-directory", "nonempty-marker-directory"])
@pytest.mark.parametrize("alias", [False, True], ids=["direct-member", "external-hardlink"])
def test_json_destination_damaged_marker_keeps_existing_package_protected(  # pylint: disable=too-many-locals
    tmp_path: Path, nested: bool, nonempty: bool, alias: bool
) -> None:
    """A real package does not become writable reporting space when its marker is damaged."""
    bundle, oracle, _objects = _bundle(tmp_path, covered=None, datasource_only=True)
    out = tmp_path / "packages"
    brief = _brief(tmp_path, UNIT)
    parent = out / "batch" if nested else out
    pkg.package_unit(bundle, UNIT, parent, oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=brief)
    package = parent / UNIT
    marker = package / "package-manifest.json"
    marker.unlink()
    marker.mkdir()
    if nonempty:
        (marker / "held.txt").write_bytes(b"damaged marker contents")
    member = package / "handover.md"
    report = tmp_path / "status.json" if alias else member
    if alias:
        os.link(member, report)
        assert report.samefile(member)
    original_report_id = report.lstat().st_ino
    original_brief = brief.read_bytes()
    original_files = {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()}
    original_dirs = {path.relative_to(out) for path in out.rglob("*") if path.is_dir()}
    with pytest.raises(SystemExit) as refused:
        pkg.main(
            [
                "--bundle",
                str(bundle),
                "--out",
                str(out),
                "--unit",
                DS_UNIT,
                "--brief",
                str(brief),
                "--json",
                str(report),
                "--discard-package-edits",
            ]
        )
    assert refused.value.code == 2
    assert brief.read_bytes() == original_brief
    assert report.lstat().st_ino == original_report_id and report.samefile(member)
    assert {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()} == original_files
    assert {path.relative_to(out) for path in out.rglob("*") if path.is_dir()} == original_dirs


@pytest.mark.parametrize("protected", ["brief", "manifest", "artifact"])
@pytest.mark.parametrize("link", ["symlink", "hardlink"])
def test_json_destination_external_alias_cannot_replace_protected_files(  # pylint: disable=too-many-locals
    tmp_path: Path, protected: str, link: str
) -> None:
    """Real native aliases must refuse even though the caller spells an external reporting path."""
    bundle, oracle, _objects = _bundle(tmp_path, covered=None, datasource_only=True)
    out = tmp_path / "packages"
    brief = _brief(tmp_path, UNIT)
    pkg.package_unit(bundle, UNIT, out, oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=brief)
    target = {
        "brief": brief,
        "manifest": out / UNIT / "package-manifest.json",
        "artifact": out / UNIT / "handover.md",
    }[protected]
    report = tmp_path / "external-status.json"
    if link == "hardlink":
        os.link(target, report)
    else:
        try:
            report.symlink_to(target)
        except (OSError, NotImplementedError) as error:
            if (
                isinstance(error, OSError)
                and error.errno not in (errno.EACCES, errno.EPERM, errno.ENOTSUP)
                and getattr(error, "winerror", None) != 1314
            ):
                raise
            pytest.skip("this platform/account cannot create symlinks without elevation")
    assert report.samefile(target), "the negative control must be an actual filesystem alias"
    original_brief = brief.read_bytes()
    original_report_id = report.lstat().st_ino
    original_files = {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()}
    original_dirs = {path.relative_to(out) for path in out.rglob("*") if path.is_dir()}
    with pytest.raises(SystemExit) as refused:
        pkg.main(["--bundle", str(bundle), "--out", str(out), "--brief", str(brief), "--json", str(report)])
    assert refused.value.code == 2
    assert brief.read_bytes() == original_brief
    assert report.lstat().st_ino == original_report_id and report.samefile(target)
    assert {path.relative_to(out): path.read_bytes() for path in out.rglob("*") if path.is_file()} == original_files
    assert {path.relative_to(out) for path in out.rglob("*") if path.is_dir()} == original_dirs


def test_json_destination_reparse_parent_is_not_traversed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The no-follow check must precede even lstat of a child reached through a directory alias."""
    bundle, _oracle, _objects = _bundle(tmp_path, covered=None, datasource_only=True)
    out = tmp_path / "packages"
    brief = _brief(tmp_path, UNIT)
    protected = tmp_path / "protected"
    protected.mkdir()
    sentinel = protected / "held.txt"
    sentinel.write_bytes(b"outside data")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(protected, target_is_directory=True)
    except (OSError, NotImplementedError) as error:
        if (
            isinstance(error, OSError)
            and error.errno not in (errno.EACCES, errno.EPERM, errno.ENOTSUP)
            and getattr(error, "winerror", None) != 1314
        ):
            raise
        pytest.skip("this platform/account cannot create symlinks without elevation")
    original = Path.lstat

    def no_follow(path: Path) -> os.stat_result:
        assert path == alias or not path.is_relative_to(alias), "report admission traversed an unsafe parent"
        return original(path)

    monkeypatch.setattr(Path, "lstat", no_follow)
    with pytest.raises(SystemExit) as refused:
        pkg.main(
            [
                "--bundle",
                str(bundle),
                "--out",
                str(out),
                "--brief",
                str(brief),
                "--json",
                str(alias / "new" / "status.json"),
            ]
        )
    assert refused.value.code == 2
    assert sentinel.read_bytes() == b"outside data" and list(protected.iterdir()) == [sentinel]
    assert not out.exists()


@pytest.mark.parametrize(
    ("unit", "scope", "code"),
    [("OtherUnit", "model_and_report", "brief_unit_mismatch"), (UNIT, "model_only", "brief_scope_mismatch")],
)
def test_wrong_brief_identity_refuses_before_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unit: str, scope: str, code: str
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    brief = _brief(tmp_path, unit, scope)
    assembled = []
    original = pkg._assemble_unit

    def observed(*args, **kwargs):
        assembled.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(pkg, "_assemble_unit", observed)
    with pytest.raises(pkg.PackagingError, match=f"^{code}$"):
        pkg.package_unit(
            bundle, UNIT, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=brief
        )
    assert assembled == [], "brief identity must be checked before any package assembly"
    assert not (tmp_path / "out" / UNIT).exists()


@pytest.mark.parametrize(
    "variant", ["profile", "build-drive", "unc", "posix", "encoded", "credential", "wire", "header"]
)
def test_full_brief_is_refused_without_copying_redacting_or_echoing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], variant: str
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    brief = _brief(tmp_path, UNIT)
    token = "s2-fake-pat-with-specials<&>"
    monkeypatch.setenv("TABLEAU_PAT_SECRET", token)
    host = "C:" + "\\" + "Users" + "\\" + "s2-private-user" + "\\private.txt"
    disclosures = {
        "profile": host,
        "build-drive": r"D:\builds\private.txt",
        "unc": r"\\s2-host\share\private.txt",
        "posix": "/var/lib/tableau/private.txt",
        "encoded": quote(host, safe=""),
        "credential": token,
        "wire": html.escape(token, quote=False),
        "header": "X-Tableau-Auth: s2-private-header",
    }
    unsafe = brief.read_text(encoding="utf-8") + "\nA final paragraph, not frontmatter: " + disclosures[variant]
    brief.write_text(unsafe, encoding="utf-8")
    original = brief.read_bytes()

    with pytest.raises(pkg.PackagingError, match="^brief_contains_unsafe_text$") as caught:
        pkg.package_unit(
            bundle, UNIT, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=brief
        )

    assert brief.read_bytes() == original, "the caller's brief must not be redacted or rewritten"
    assert not (tmp_path / "out" / UNIT).exists(), "private brief bytes must never ship"
    output = str(caught.value) + str(capsys.readouterr())
    assert disclosures[variant] not in output
    assert str(brief) not in output


@pytest.mark.parametrize("via_cli", [False, True], ids=["constructor", "cli"])
def test_brief_copy_uses_the_same_bytes_that_passed_preassembly_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, via_cli: bool
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    brief = _brief(tmp_path, UNIT)
    expected = brief.read_bytes()
    original = pkg._assemble_unit

    def changed_after_validation(*args, **kwargs):
        brief.write_text("X-Tableau-Auth: a-new-private-token", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(pkg, "_assemble_unit", changed_after_validation)
    if via_cli:
        assert (
            pkg.main(
                [
                    "--bundle",
                    str(bundle),
                    "--out",
                    str(tmp_path / "out"),
                    "--unit",
                    UNIT,
                    "--oracle",
                    str(oracle),
                    "--assets",
                    str(bundle.parent / "assets"),
                    "--brief",
                    str(brief),
                    "--quiet",
                ]
            )
            == 0
        )
    else:
        pkg.package_unit(
            bundle, UNIT, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=brief
        )

    assert (tmp_path / "out" / UNIT / "migration-brief.md").read_bytes() == expected
    assert pkg.data_access.read_data_access(tmp_path / "out" / UNIT / "data-access.json").state == "local_import_ready"


def test_new_package_seals_exact_data_access_bytes_and_exposes_pending_consumer(tmp_path: Path) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    result = pkg.package_unit(
        bundle,
        UNIT,
        tmp_path / "out",
        oracle_dir=oracle,
        assets_dir=bundle.parent / "assets",
        brief=_brief(tmp_path, UNIT),
    )
    package = tmp_path / "out" / UNIT
    wire = (package / "data-access.json").read_bytes()
    assessment = pkg.data_access.parse_data_access(wire.decode("utf-8"))
    assert assessment.state == "local_import_ready"
    assert assessment.codes == ("all-flat-file", "package-self-contained")
    assert result["artifacts"]["data_access"] == "data-access.json"
    assert result["contents"]["files"]["data-access.json"] == hashlib.sha256(wire).hexdigest()
    assert wire == assessment.dumps().encode("utf-8") and wire.endswith(b"\n") and b"\r" not in wire
    assert pkg.pri.verify_s1(package).integrity.is_clean
    for name in ("README.md", "handover.md"):
        text = (package / name).read_text(encoding="utf-8")
        assert "state=local_import_ready" in text
        assert pkg.DATA_ACCESS_PENDING in text
    rendered = pkg.render([result], tmp_path / "out", [], [], [UNIT])
    assert "state=local_import_ready" in rendered and pkg.DATA_ACCESS_PENDING in rendered


@pytest.mark.parametrize("variant", ["plain", "legacy", "missing"])
def test_producer_keeps_a_strict_cannot_file_when_policy_is_unparsed(tmp_path: Path, variant: str) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    brief = _brief(tmp_path, UNIT)
    if variant == "legacy":
        brief.write_text(
            brief.read_text(encoding="utf-8").replace('fallback_authorization = "stop"\n', ""), encoding="utf-8"
        )
    elif variant == "plain":
        brief.write_text("Stop unless somebody might authorize model-only.", encoding="utf-8")
    result = pkg.package_unit(
        bundle,
        UNIT,
        tmp_path / "out",
        oracle_dir=oracle,
        assets_dir=bundle.parent / "assets",
        brief=None if variant == "missing" else brief,
    )
    package = tmp_path / "out" / UNIT
    assessment = pkg.data_access.read_data_access(package / "data-access.json")
    assert (assessment.state, assessment.codes) == ("cannot_establish", ("projection-invalid",))
    assert assessment.source_keys == () and assessment.effective_scope is None
    assert "DATA_ACCESS limitation=brief_policy_not_parsed" in result["notes"]
    assert result["construction_status"] == pkg.STATUS_ASSEMBLED
    assert result["has_engine_working_copy"] is True and result["self_contained"] is True
    assert result["dispatch_readiness"]["status"] == pkg.DISPATCH_READINESS_NOT_EVALUATED
    assert pkg.pri.verify_s1(package).integrity.is_clean


def _reseal(package: Path) -> None:
    """Use S2's independent fixture seal, not the producer's contents writer."""
    s2.seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))


def _assessment_candidate(parent: Path, spec: dict, *, datasource: bool = False, fallback: str = "stop") -> Path:
    """Hand-built S1/S2 roles with supplied spec facts; no producer-generated expectations."""
    package = (s2.datasource_package if datasource else s2.workbook_package)(parent / "candidate")
    payload = json.loads((package / "migration-spec.json").read_text(encoding="utf-8"))
    payload["data_sources"] = copy.deepcopy(spec["data_sources"])
    s2._write(package / "migration-spec.json", payload)
    brief = package / "migration-brief.md"
    brief.write_text(brief.read_text(encoding="utf-8").replace('"stop"', json.dumps(fallback)), encoding="utf-8")
    _reseal(package)
    return package


def _assess_candidate(
    package: Path, root: Path, *, local: dict | None = None, providers: tuple[Path, ...] = ()
) -> pkg.data_access.DataAccessAssessment:
    assessment, _notes = pkg._assess_package_data_access(
        root,
        package,
        copy.deepcopy(authority.LOCAL) if local is None else local,
        gate_root=root,
        provider_packages=providers,
    )
    assert pkg.data_access.parse_data_access(assessment.dumps()) == assessment
    return assessment


def test_packaged_live_spec_uses_pure_parser_validation_not_the_gate_arming_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = tmp_path / "Live.tds"
    asset.write_text(
        "<datasource name='Live'><connection class='sqlserver' server='source.example' dbname='db'/></datasource>",
        encoding="utf-8",
    )
    actual_run = subprocess.run
    calls = []

    def refuse_lifecycle(command, **kwargs):
        assert command[1] != str(pkg.SCRIPT_DIR / "parse_tableau.py"), (
            "parser CLI arms a SECOND gate at the staged spec"
        )
        calls.append(kwargs["timeout"])
        return actual_run(command, **kwargs)

    monkeypatch.setattr(pkg.subprocess, "run", refuse_lifecycle)
    assert pkg._write_spec(asset, tmp_path) == ("migration-spec.json", None)
    spec = json.loads((tmp_path / "migration-spec.json").read_text(encoding="utf-8"))
    assert spec["data_sources"][0]["connection"]["server"] == "source.example"
    assert spec["data_sources"][0]["connection"]["class"] == "sqlserver"
    assert calls == [600], "the bounded parser subprocess was lost"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["Live.tds", "migration-spec.json"]


@pytest.mark.parametrize("missing", [False, True])
def test_real_localized_bytes_and_exact_in_memory_facts_drive_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    inputs = tmp_path / "input-data"
    inputs.mkdir()
    expected = b"value\n7\n"
    paths = [inputs / "one.csv", inputs / "two.csv"]
    paths[0].write_bytes(expected)
    if not missing:
        paths[1].write_bytes(expected + b"8\n")
    tables = bundle / "pbip" / UNIT / f"{UNIT}.SemanticModel" / "definition" / "tables"
    tables.mkdir()
    for index, path in enumerate(paths):
        (tables / f"Input{index}.tmdl").write_text(
            f"table Input{index}\n\tpartition Input{index} = m\n\t\tmode: import\n"
            f'\t\tsource = Csv.Document(File.Contents("{path}"))\n',
            encoding="utf-8",
        )
    localized = []
    assessed = []
    localize = pkg._localize_data_sources
    assess = pkg.data_access.assess_data_access

    def localize_once(*args):
        facts = localize(*args)
        localized.append(facts)
        return facts

    def same_facts(root, **kwargs):
        assert kwargs["package_data_sources"] == localized[0]
        assert kwargs["package_data_sources"] is not localized[0], "assessment must use a held copy"
        assessed.append(root)
        return assess(root, **kwargs)

    monkeypatch.setattr(pkg, "_localize_data_sources", localize_once)
    monkeypatch.setattr(pkg.data_access, "assess_data_access", same_facts)
    result = pkg.package_unit(
        bundle,
        UNIT,
        tmp_path / "out",
        oracle_dir=oracle,
        assets_dir=bundle.parent / "assets",
        brief=_brief(tmp_path, UNIT),
    )
    package = tmp_path / "out" / UNIT
    projection = pkg.data_access.read_data_access(package / "data-access.json")
    assert (projection.state, projection.codes) == (
        ("blocked", ("local-import-incomplete",))
        if missing
        else ("local_import_ready", ("all-flat-file", "package-self-contained"))
    )
    assert assessed == [bundle.resolve()]
    assert result["data_sources"] == localized[0]
    assert result["data_sources"]["binding"] is not None, "unbound is not the same as missing bytes"
    assert len(result["data_sources"]["shipped"]) == (1 if missing else 2)
    for index, row in enumerate(result["data_sources"]["shipped"]):
        shipped = (package / row["path"]).read_bytes()
        assert shipped == (expected if index == 0 else expected + b"8\n")
        assert result["contents"]["files"][row["path"]] == hashlib.sha256(shipped).hexdigest()
    assert pkg.pri.verify_s1(package).integrity.is_clean


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize(
    ("fault", "state", "codes"),
    [
        ("none", "live_data_ok", ("probe-cleared", "probe-data-ok")),
        ("missing", "cannot_establish", ("audit-missing",)),
        ("unkeyed", "blocked", ("stale-clear",)),
        ("manual", "blocked", ("manual-clear",)),
        ("rearm", "blocked", ("marker-only",)),
    ],
)
def test_producer_uses_current_keyed_authority_without_writes(
    tmp_path: Path, root: Path, fault: str, state: str, codes: tuple[str, ...]
) -> None:
    authority._earn(root)
    rows = authority._rows(root)
    if fault == "missing":
        (root / authority.gate.AUDIT).unlink()
    elif fault == "unkeyed":
        for row in rows:
            if row["action"].startswith("probe-"):
                row.pop("sources", None)
        authority._write_rows(root, rows)
    elif fault == "manual":
        authority._write_rows(root, [row for row in rows if not row["action"].startswith("probe-")])
        authority.gate._audit(root, "manual-clear", "fixture-only")
    elif fault == "rearm":
        authority.gate._audit(root, "block-marker-only", "sources_json=" + json.dumps([authority.KEY]), [authority.KEY])
    candidate = _assessment_candidate(tmp_path, authority._spec(authority.LIVE))
    before = {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()}
    actual = _assess_candidate(candidate, root)
    assert (actual.state, actual.codes) == (state, codes)
    assert actual.source_keys == (() if state == "cannot_establish" else (authority.KEY,))
    assert {path.name: path.read_bytes() for path in root.iterdir() if path.is_file()} == before


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("fault", ["none", "unearned-key", "incomplete-flat"])
def test_mixed_direct_sources_need_every_key_and_all_local_bytes(
    tmp_path: Path, root: Path, reverse: bool, fault: str
) -> None:
    keys = [authority.KEY, authority.OTHER_KEY]
    connections = [authority.FLAT, authority.LIVE, authority.OTHER]
    if reverse:
        keys.reverse()
        connections.reverse()
    assert authority.gate.apply_block(root, keys) == 0
    for key in keys:
        if fault == "unearned-key" and key == authority.OTHER_KEY:
            continue
        authority.gate._audit(root, "probe-data_ok", "fixture row returned", [key])
        assert authority.gate.clear_block(root, "fixture-earned", earned=True, sources=[key]) == 0
    candidate = _assessment_candidate(tmp_path, authority._spec(*connections))
    local = copy.deepcopy(authority.LOCAL)
    if fault == "incomplete-flat":
        local["omissions"] = [{"file": "not-shipped.csv", "reason": "missing"}]
    result = _assess_candidate(candidate, root, local=local)
    assert result.source_keys == tuple(sorted(keys))
    assert (result.state, result.codes) == {
        "none": ("live_data_ok", ("probe-cleared", "probe-data-ok")),
        "unearned-key": ("blocked", ("marker-only",)),
        "incomplete-flat": ("blocked", ("local-import-incomplete",)),
    }[fault]


@pytest.mark.parametrize(
    ("fault", "expected"),
    [
        ("none", ("authorized_model_only", ("brief-model-only", "human-authorize"))),
        ("bare-override", ("cannot_establish", ("audit-missing",))),
        ("stop", ("blocked", ("authorization-mismatch", "stale-clear"))),
        ("workbook", ("blocked", ("authorization-mismatch", "stale-clear"))),
        ("review", ("blocked", ("stale-clear", "unknown-target"))),
        ("local-missing", ("blocked", ("local-import-incomplete", "stale-clear"))),
    ],
)
def test_authorization_never_downgrades_scope_or_overrides_missing_prerequisites(
    tmp_path: Path, root: Path, fault: str, expected: tuple
) -> None:
    authority._authorize(root)
    if fault == "bare-override":
        (root / authority.gate.AUDIT).unlink()
    connections = [authority.LIVE]
    if fault == "review":
        connections.append(authority.REVIEW)
    candidate = _assessment_candidate(
        tmp_path,
        authority._spec(*connections),
        datasource=fault != "workbook",
        fallback="stop" if fault == "stop" else "model_only_unvalidated",
    )
    local = copy.deepcopy(authority.LOCAL)
    if fault == "local-missing":
        local["neutralized"] = ["missing.csv"]
    result = _assess_candidate(candidate, root, local=local)
    assert (result.state, result.codes) == expected
    if fault == "none":
        assert (result.effective_scope, result.validation, result.max_phase2_claim) == (
            "model_only",
            "unvalidated",
            "structural_only",
        )
    else:
        assert result.effective_scope is None and result.max_phase2_claim == "none"


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("mixed-malformed", "audit-malformed"),
        ("duplicate-key", "audit-malformed"),
        ("missing-field", "audit-malformed"),
        ("foreign", "audit-foreign-scope"),
        ("future", "audit-malformed"),
    ],
)
def test_producer_preserves_the_authoritys_exact_audit_refusal(
    tmp_path: Path, root: Path, fault: str, code: str
) -> None:
    authority._earn(root)
    candidate = _assessment_candidate(tmp_path, authority._spec(authority.LIVE))
    assert _assess_candidate(candidate, root).state == "live_data_ok"
    rows = authority._rows(root)
    if fault == "mixed-malformed":
        rows.append({"action": "probe-data_ok", "scope": str(root.resolve())})
    elif fault == "missing-field":
        rows[-1].pop("user")
    elif fault == "foreign":
        rows[-1]["scope"] = str(root.parent / "foreign")
    elif fault == "future":
        rows[-1]["ts"] = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    authority._write_rows(root, rows)
    if fault == "duplicate-key":
        with (root / authority.gate.AUDIT).open("a", encoding="utf-8") as handle:
            handle.write('{"action":"probe-data_ok","action":"probe-cleared"}\n')
    result = _assess_candidate(candidate, root)
    assert (result.state, result.codes, result.source_keys) == ("cannot_establish", (code,), ())


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize("change", ["root-superset", "new-package-key", "changed-root-key", "spec-tamper"])
def test_gate_key_coverage_allows_supersets_but_not_foreign_or_unsealed_sources(
    tmp_path: Path, root: Path, change: str
) -> None:
    authority._earn(root)
    candidate = _assessment_candidate(tmp_path, authority._spec(authority.LIVE))
    assert _assess_candidate(candidate, root).source_keys == (authority.KEY,), "root already has an extra key"
    if change == "new-package-key":
        foreign = {**authority.LIVE, "server": "uncovered.example"}
        spec = json.loads((candidate / "migration-spec.json").read_text(encoding="utf-8"))
        spec["data_sources"] = authority._spec(foreign)["data_sources"]
        s2._write(candidate / "migration-spec.json", spec)
        _reseal(candidate)
    elif change == "changed-root-key":
        s2._write(root / "migration-spec.json", authority._spec(authority.OTHER))
    elif change == "spec-tamper":
        with (candidate / "migration-spec.json").open("a", encoding="utf-8") as handle:
            handle.write(" ")
        assert pkg.pri.verify_s1(candidate).integrity.codes() == ("package_file_digest_mismatch",)
    result = _assess_candidate(candidate, root)
    assert (result.state, result.codes) == {
        "root-superset": ("live_data_ok", ("probe-cleared", "probe-data-ok")),
        "new-package-key": ("cannot_establish", ("source-key-set-changed",)),
        "changed-root-key": ("cannot_establish", ("source-key-set-changed",)),
        "spec-tamper": ("cannot_establish", ("projection-invalid",)),
    }[change]


LOCAL_PROJECTION = {
    "schema": "phase1-data-access/v1",
    "state": "local_import_ready",
    "source_keys": [],
    "provider_unit": None,
    "provider_state": None,
    "validation": "validated",
    "effective_scope": "model_only",
    "max_phase2_claim": "data_validated",
    "codes": ["all-flat-file", "package-self-contained"],
}


def _projection_fixture(package: Path, payload: dict) -> None:
    """Independent wire bytes and artifact declaration, sealed by the existing S2 fixture."""
    (package / "data-access.json").write_bytes(
        (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    )
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"]["data_access"] = "data-access.json"
    s2.seal(package, **manifest)


def _direct_provider(
    parent: Path, *, luid: str = DS_LUID, key: str = authority.KEY, connection: dict[str, Any] | None = None
) -> Path:
    """Supply direct connection metadata independently of the projection's claimed key."""
    package = s2.datasource_package(parent, unit="Shared", luid=luid, published_key=s2.PUBLISHED_KEY)
    spec_path = package / "migration-spec.json"
    spec = json.loads(spec_path.read_bytes())
    spec["data_sources"][0]["connection"] = dict(authority.LIVE if connection is None else connection)
    s2._write(spec_path, spec)
    _projection_fixture(
        package,
        {
            **LOCAL_PROJECTION,
            "state": "live_data_ok",
            "source_keys": [key],
            "codes": ["probe-cleared", "probe-data-ok"],
        },
    )
    return package


def _provider_consumer(parent: Path, provider: Path) -> Path:
    package = parent / "Consumer"
    binding = os.path.relpath(provider / "fabric" / "Shared.SemanticModel", package / "fabric" / "Revenue.Report")
    return s2.workbook_package(package, published={"key": s2.PUBLISHED_KEY}, binding=binding.replace("\\", "/"))


@pytest.mark.parametrize("change", ["projection-key", "connection-identity", "missing-connection"])
def test_direct_provider_projection_is_grounded_in_current_held_metadata(tmp_path: Path, change: str) -> None:
    """A one-sided identity change reaches binding authority after clean S1/S2 and legal projection parsing."""
    provider = _direct_provider(tmp_path / "provider", key="source-key:ab1baa4b3f77bb70")
    held = pkg._binding_hold(provider)
    role = pkg.pri.verify_phase1_role_identity([provider])[0]
    assert role.verified is not None and role.verified.integrity.is_clean
    assert role.is_start_ready, role.codes()
    baseline = role.data_access_handoff(provider)
    assert isinstance(baseline, pkg.pri.PackageDataAccessHandoff)
    projection = pkg.data_access.parse_data_access(baseline.data_access.content.decode("utf-8"))
    assert projection.state == "live_data_ok"
    assert projection.source_keys == baseline.facts.live_source_keys == ("source-key:ab1baa4b3f77bb70",), (
        "direct-provider source-key authority must derive the literal key from held connection metadata"
    )
    assert baseline.facts.direct_applicable and not baseline.facts.published_only
    assert not baseline.facts.has_review and baseline.facts.refusal_code is None
    pkg._binding_access(held, role, provider)

    expected_keys = ("source-key:ab1baa4b3f77bb70",)
    if change == "projection-key":
        payload = json.loads(baseline.data_access.content)
        payload["source_keys"] = ["source-key:e625ce798a6d19bb"]
        s2._write(provider / "data-access.json", payload)
    else:
        spec = json.loads(baseline.migration_spec.content)
        if change == "connection-identity":
            spec["data_sources"][0]["connection"]["server"] = "other.example"
            expected_keys = ("source-key:e625ce798a6d19bb",)
        else:
            del spec["data_sources"][0]["connection"]
            expected_keys = ()
        s2._write(provider / "migration-spec.json", spec)
    _reseal(provider)

    held = pkg._binding_hold(provider)
    role = pkg.pri.verify_phase1_role_identity([provider])[0]
    assert role.verified is not None and role.verified.integrity.is_clean
    assert role.is_start_ready, role.codes()
    handoff = role.data_access_handoff(provider)
    assert isinstance(handoff, pkg.pri.PackageDataAccessHandoff)
    if change == "projection-key":
        assert handoff.migration_spec.content == baseline.migration_spec.content
    else:
        assert handoff.data_access.content == baseline.data_access.content
    projection = pkg.data_access.parse_data_access(handoff.data_access.content.decode("utf-8"))
    assert projection.state == "live_data_ok"
    assert projection.source_keys == (
        "source-key:e625ce798a6d19bb" if change == "projection-key" else "source-key:ab1baa4b3f77bb70",
    )
    assert handoff.facts.live_source_keys == expected_keys
    assert handoff.facts.refusal_code == ("source-key-invalid" if change == "missing-connection" else None)
    assert handoff.facts.direct_applicable is (change != "missing-connection")
    assert not handoff.facts.published_only and not handoff.facts.has_review and not handoff.facts.all_flat
    with pytest.raises(pkg._BindingRefusal) as caught:
        pkg._binding_access(held, role, provider)
    assert (caught.value.code, caught.value.exit_code) == ("binding_source_facts_mismatch", 3)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("repeated", [False, True])
def test_inheritance_uses_exact_ordinal_not_duplicate_provider_unit_names(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool, repeated: bool
) -> None:
    selected = _direct_provider(tmp_path / "selected", key="source-key:ab1baa4b3f77bb70")
    other = _direct_provider(
        tmp_path / "other", luid=s2.WB_LUID, key="source-key:e625ce798a6d19bb", connection=authority.OTHER
    )
    consumer = _provider_consumer(tmp_path, selected)
    if repeated:
        spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
        spec["data_sources"] *= 2
        s2._write(consumer / "migration-spec.json", spec)
        _reseal(consumer)
    providers = (other, selected) if reverse else (selected, other)
    s2_results = pkg.pri.verify_phase1_role_identity([*providers, consumer])
    assert all(result.is_start_ready for result in s2_results)
    assert len(s2_results[-1].dependencies) == (2 if repeated else 1)
    assert {dep.provider_ordinal for dep in s2_results[-1].dependencies} == {1 if reverse else 0}
    for ordinal, provider in enumerate(providers):
        role = s2_results[ordinal]
        assert role.verified is not None and role.verified.integrity.is_clean
        assert role.verified.root == provider
        handoff = role.data_access_handoff(provider)
        assert isinstance(handoff, pkg.pri.PackageDataAccessHandoff)
        assert handoff.migration_spec.root_identity == handoff.data_access.root_identity == str(provider)
        connection, key = (
            (authority.LIVE, "source-key:ab1baa4b3f77bb70")
            if provider == selected
            else (authority.OTHER, "source-key:e625ce798a6d19bb")
        )
        projection = pkg.data_access.parse_data_access(handoff.data_access.content.decode("utf-8"))
        assert projection.state == "live_data_ok"
        assert projection.source_keys == handoff.facts.live_source_keys == (key,), (
            "direct-provider source-key authority must derive the literal key from held connection metadata"
        )
        assert json.loads(handoff.migration_spec.content)["data_sources"] == [{"id": "ds-1", "connection": connection}]
        assert handoff.facts.direct_applicable and not handoff.facts.published_only
        assert not handoff.facts.has_review and handoff.facts.refusal_code is None
        pkg._binding_access(pkg._binding_hold(provider), role, provider)
    references = []
    make_reference = pkg.data_access.provider_reference

    def exact_reference(unit):
        references.append(unit)
        return make_reference(unit)

    def no_provider_reassessment(*_args, **_kwargs):
        pytest.fail("inheritance re-read an original audit instead of the exact packaged provider")

    monkeypatch.setattr(pkg.data_access, "provider_reference", exact_reference)
    monkeypatch.setattr(pkg.data_access, "_read_audit_trail", no_provider_reassessment)
    result = _assess_candidate(consumer, root, providers=providers)
    expected_reference = (
        "provider-ref:v1:sha256:" + hashlib.sha256(b"phase1-data-access/provider-unit/v1\0Shared").hexdigest()
    )
    assert result.to_json() == {
        "schema": "phase1-data-access/v1",
        "state": "provider_inherited",
        "source_keys": ["source-key:ab1baa4b3f77bb70"],
        "provider_unit": expected_reference,
        "provider_state": "live_data_ok",
        "validation": "validated",
        "effective_scope": "report_only_shared_model",
        "max_phase2_claim": "data_validated",
        "codes": ["provider-exact"],
    }
    assert references == ["Shared"], "hash the exact selected unit once, never the already-hashed token"
    assert "Shared" not in result.dumps()


@pytest.mark.parametrize(
    ("fault", "state", "code", "s2_code"),
    [
        ("missing", "cannot_establish", "provider-missing", "provider_missing"),
        ("ambiguous", "cannot_establish", "provider-ambiguous", "provider_ambiguous"),
        ("s2-blocked", "cannot_establish", "provider-missing", "provider_not_s2_clean"),
        ("wrong-binding", "cannot_establish", "provider-foreign", "provider_binding_mismatch"),
        ("blocked", "cannot_establish", "provider-missing", None),
        ("cannot", "cannot_establish", "provider-missing", None),
        ("recursive", "cannot_establish", "provider-ambiguous", None),
        ("authorized", "blocked", "provider-model-only", None),
        ("unauthorized-policy", "cannot_establish", "provider-foreign", None),
        ("scope-mismatch", "cannot_establish", "provider-foreign", None),
        ("undeclared", "cannot_establish", "provider-missing", None),
        ("malformed", "cannot_establish", "projection-invalid", None),
        ("tampered", "cannot_establish", "provider-missing", "provider_missing"),
        ("repeated-invalid", "cannot_establish", "projection-invalid", "published_dependency_invalid"),
    ],
)
def test_provider_limitations_and_missing_or_untrusted_inputs_never_inherit(
    tmp_path: Path, root: Path, fault: str, state: str, code: str, s2_code: str | None
) -> None:
    provider = _direct_provider(tmp_path / "provider")
    consumer = _provider_consumer(tmp_path, provider)
    providers = [provider]
    payload = json.loads((provider / "data-access.json").read_text(encoding="utf-8"))
    if fault in ("blocked", "cannot"):
        payload.update(
            state="blocked" if fault == "blocked" else "cannot_establish",
            source_keys=[],
            validation="not_established",
            effective_scope=None,
            max_phase2_claim="none",
            codes=["marker-only" if fault == "blocked" else "projection-invalid"],
        )
    elif fault in ("authorized", "unauthorized-policy"):
        payload.update(
            state="authorized_model_only",
            validation="unvalidated",
            max_phase2_claim="structural_only",
            codes=["brief-model-only", "human-authorize"],
        )
        if fault == "authorized":
            brief = provider / "migration-brief.md"
            brief.write_text(
                brief.read_text(encoding="utf-8").replace('"stop"', '"model_only_unvalidated"'), encoding="utf-8"
            )
    elif fault == "recursive":
        payload.update(
            state="provider_inherited",
            provider_unit="provider-ref:v1:sha256:" + "a" * 64,
            provider_state="live_data_ok",
            codes=["provider-exact"],
        )
    elif fault == "scope-mismatch":
        payload["effective_scope"] = "model_and_report"
    _projection_fixture(provider, payload)
    if fault == "missing":
        providers = []
    elif fault == "ambiguous":
        providers.append(_direct_provider(tmp_path / "duplicate"))
    elif fault == "s2-blocked":
        (provider / "migration-brief.md").unlink()
    elif fault == "wrong-binding":
        path = consumer / "fabric" / "Revenue.Report" / "definition.pbir"
        s2._write(path, {"datasetReference": {"byPath": {"path": "../../../wrong/fabric/Shared.SemanticModel"}}})
        manifest = json.loads((consumer / "package-manifest.json").read_text(encoding="utf-8"))
        manifest["model_binding"]["path"] = "../../../wrong/fabric/Shared.SemanticModel"
        s2.seal(consumer, **manifest)
    elif fault == "undeclared":
        manifest = json.loads((provider / "package-manifest.json").read_text(encoding="utf-8"))
        manifest["artifacts"].pop("data_access")
        s2.seal(provider, **manifest)
    elif fault == "malformed":
        (provider / "data-access.json").write_text('{"state":"live_data_ok","state":"blocked"}', encoding="utf-8")
    elif fault == "tampered":
        data_file = provider / "data-access.json"
        data_file.write_bytes(data_file.read_bytes() + b" ")
    elif fault == "repeated-invalid":
        spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
        spec["data_sources"].append({"published_datasource": None})
        s2._write(consumer / "migration-spec.json", spec)
    if fault != "tampered":
        _reseal(provider)
    _reseal(consumer)
    s2_results = pkg.pri.verify_phase1_role_identity([*providers, consumer])
    if s2_code is not None:
        assert s2_code in s2_results[-1].codes()
    else:
        assert s2_results[-1].is_start_ready, s2_results[-1].codes()
    result = _assess_candidate(consumer, root, providers=tuple(providers))
    assert (result.state, result.codes) == (state, (code,))
    assert result.provider_unit is None and result.max_phase2_claim == "none"


@pytest.mark.parametrize("placement", ["additional-row", "published-row", "nested-leg"])
def test_mixed_published_and_direct_consumers_refuse_the_authoritys_inheritance_shortcut(
    tmp_path: Path, root: Path, placement: str
) -> None:
    provider = _direct_provider(tmp_path / "provider")
    consumer = _provider_consumer(tmp_path, provider)
    assert _assess_candidate(consumer, root, providers=(provider,)).state == "provider_inherited"
    spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
    if placement == "additional-row":
        spec["data_sources"].extend(authority._spec(authority.LIVE)["data_sources"])
    elif placement == "published-row":
        spec["data_sources"][0]["connection"] = dict(authority.LIVE)
    else:
        spec["data_sources"][0]["connection"] = {"class": "sqlproxy", "connections": [dict(authority.LIVE)]}
    s2._write(consumer / "migration-spec.json", spec)
    _reseal(consumer)
    local = {**authority.LOCAL, "self_contained": False, "omissions": [{"file": "missing.csv"}]}
    provider_pair = (
        pkg.data_access.provider_reference("Shared"),
        pkg.data_access.read_data_access(provider / "data-access.json"),
    )
    assert (
        pkg.data_access.assess_data_access(
            root,
            package_spec=spec,
            package_data_sources=local,
            fallback_authorization="stop",
            requested_scope="report_only_shared_model",
            provider=provider_pair,
        ).state
        == "provider_inherited"
    ), "independent authority control: it does not inspect these direct legs"
    result = _assess_candidate(consumer, root, local=local, providers=(provider,))
    assert (result.state, result.codes) == ("cannot_establish", ("projection-invalid",))


def test_contradictory_luid_and_second_dependency_cannot_be_collapsed_by_name(tmp_path: Path, root: Path) -> None:
    selected = _direct_provider(tmp_path / "selected")
    other = _direct_provider(tmp_path / "other", luid=s2.WB_LUID, key=authority.OTHER_KEY, connection=authority.OTHER)
    consumer = _provider_consumer(tmp_path, selected)
    spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
    spec["data_sources"].append(
        {
            "connection": {"class": "sqlproxy", "mode": "live"},
            "published_datasource": {"luid": s2.WB_LUID, "key": s2.PUBLISHED_KEY},
        }
    )
    s2._write(consumer / "migration-spec.json", spec)
    provenance = json.loads((consumer / "source-provenance.json").read_bytes())
    rows = provenance["inputs"][0]["origin"]["published_dependencies"]["rows"]
    rows.append({**rows[0], "source_ordinal": 1, "datasource_luid": s2.WB_LUID})
    s2._write(consumer / "source-provenance.json", provenance)
    _reseal(consumer)
    verdict = pkg.pri.verify_phase1_role_identity([selected, other, consumer])[-1]
    assert [dep.state for dep in verdict.dependencies] == ["resolved", "mismatch"]
    assert verdict.dependencies[1].code == "provider_binding_mismatch"
    result = _assess_candidate(consumer, root, providers=(selected, other))
    assert (result.state, result.codes) == ("cannot_establish", ("provider-foreign",))


def _shared_bundle(parent: Path) -> tuple[Path, Path]:
    """Real parsed published datasource/consumer bytes, with an independently specified PBIR binding."""
    bundle, oracle, _objects = _bundle(parent, covered=None, datasource_only=True)
    assets = bundle.parent / "assets"
    consumer = next(assets.glob(f"*_{UNIT}.twb"))
    shutil.copyfile(Path(__file__).parent / "fixtures" / "published_datasource.twb", consumer)
    provider = assets / f"{DS_LUID}_{DS_UNIT}.tds"
    provider.write_text(
        "<datasource name='Shared'>"
        "<repository-location id='SalesMaster' site='Finance'/>"
        "<connection class='excel-direct'><relation name='Rows' table='[Rows]' type='table'/></connection>"
        "</datasource>",
        encoding="utf-8",
    )
    _write_input_manifest(bundle, [consumer, provider])
    provenance = json.loads((bundle / "source-provenance.json").read_text(encoding="utf-8"))
    source = provenance["inputs"][0]
    source["input"]["sha256"] = hashlib.sha256(consumer.read_bytes()).hexdigest()
    source["origin"]["published_dependencies"] = s2.published_authority(
        source["input"]["sha256"], source["origin"]["workbook_luid"]
    )
    source["origin"]["published_dependencies"]["rows"][0]["published_key"] = "finance/salesmaster"
    provenance["inputs"].append(
        {
            "input": {"file": provider.name, "sha256": hashlib.sha256(provider.read_bytes()).hexdigest()},
            "origin": {"datasource_luid": DS_LUID, "match": "sha256"},
        }
    )
    s2._write(bundle / "source-provenance.json", provenance)
    shutil.rmtree(bundle / "pbip" / UNIT / f"{UNIT}.SemanticModel")
    s2._write(
        bundle / "pbip" / UNIT / f"{UNIT}.Report" / "definition.pbir",
        {
            "version": "4.0",
            "datasetReference": {"byPath": {"path": f"../../../{DS_UNIT}/fabric/{DS_UNIT}.SemanticModel"}},
        },
    )
    _write_receipt(bundle, [UNIT, DS_UNIT])
    return bundle, oracle


def _package_command(bundle: Path, oracle: Path, out: Path, unit: str, brief: Path) -> list[str]:
    return [
        "--bundle",
        str(bundle),
        "--out",
        str(out),
        "--unit",
        unit,
        "--oracle",
        str(oracle),
        "--assets",
        str(bundle.parent / "assets"),
        "--brief",
        str(brief),
        "--quiet",
    ]


@pytest.mark.parametrize(
    ("fault", "expected_exit", "reason"),
    [
        (None, 0, None),
        ("wrong-basename", 5, "input_manifest_path_mismatch"),
        ("duplicate-row", 5, "source_asset_row_ambiguous"),
        ("duplicate-candidate", 5, "source_asset_candidate_ambiguous"),
        ("changed-digest", 5, "input_manifest_digest_mismatch"),
        ("foreign-path", 6, "unassessable_input"),
        ("missing-candidate", 6, "unassessable_input"),
    ],
    ids=[
        "valid",
        "wrong-basename",
        "duplicate-row",
        "duplicate-candidate",
        "changed-digest",
        "foreign-path",
        "missing",
    ],
)
def test_canonical_datasource_model_only_package_publishes_from_staged_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fault: str | None, expected_exit: int, reason: str | None
) -> None:
    bundle, oracle = _shared_bundle(tmp_path)
    asset = bundle.parent / "assets" / f"{DS_LUID}_{DS_UNIT}.tds"
    row = {"name": DS_UNIT, "staged_input_path": str(asset), "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}
    rows = [row]
    if fault == "wrong-basename":
        other = asset.with_name("Other.tds")
        shutil.copyfile(asset, other)
        row["staged_input_path"] = str(other)
    elif fault == "duplicate-row":
        rows.append(dict(row))
    elif fault == "duplicate-candidate":
        second = bundle / "assets" / asset.name
        second.parent.mkdir()
        shutil.copyfile(asset, second)
    elif fault == "changed-digest":
        asset.write_text("<datasource/>", encoding="utf-8")
    elif fault == "foreign-path":
        row["staged_input_path"] = (
            f"/foreign/assets/{asset.name}" if os.name == "nt" else f"C:\\foreign\\assets\\{asset.name}"
        )
    elif fault == "missing-candidate":
        asset.unlink()
    write_rows(bundle, rows)
    brief = _brief(tmp_path, DS_UNIT, "model_only", numeric_obligation="none")
    out = tmp_path / "out"

    assert pkg.main(_package_command(bundle, oracle, out, DS_UNIT, brief)) == expected_exit
    package = out / DS_UNIT
    if fault is not None:
        assert reason in capsys.readouterr().err
        assert not package.exists(), "a refused source cannot publish a diagnostic package"
        assert not pkg.staging_dir(out, DS_UNIT).exists()
        return
    manifest = json.loads((package / "package-manifest.json").read_bytes())
    assert manifest["construction_status"] == pkg.STATUS_ASSEMBLED
    assert manifest["artifacts"]["asset"] == f"assets/{asset.name}"
    assert (package / "assets" / asset.name).read_bytes() == asset.read_bytes()
    assert (package / "migration-brief.md").read_bytes() == brief.read_bytes()
    assert pkg.data_access.read_data_access(package / "data-access.json").effective_scope == "model_only"
    assert pkg.pri.verify_s1(package).integrity.is_clean
    assert pkg.pri.verify_phase1_role_identity([package])[0].is_start_ready


@pytest.mark.parametrize("locator", ["absolute", "basename"])
def test_canonical_shared_packages_resolve_raw_sources_and_ship_redacted_handover(tmp_path: Path, locator: str) -> None:
    bundle, oracle = _shared_bundle(tmp_path)
    assets = bundle.parent / "assets"
    consumer = next(assets.glob(f"*_{UNIT}.twb"))
    provider = assets / f"{DS_LUID}_{DS_UNIT}.tds"
    write_rows(
        bundle,
        [
            {"name": unit, "staged_input_path": str(asset), "sha256": hashlib.sha256(asset.read_bytes()).hexdigest()}
            for unit, asset in ((DS_UNIT, provider), (UNIT, consumer))
        ],
    )
    handover_path = bundle / "handover" / f"{UNIT}.json"
    raw = json.loads(handover_path.read_bytes())
    source_id = str(consumer) if locator == "absolute" else consumer.name
    raw["workbook"]["source_id"] = source_id
    private_path = str(tmp_path / "private-host-canary" / "unshipped.txt")
    raw["workbook"]["private_path"] = private_path
    s2._write(handover_path, raw)
    original_handover = handover_path.read_bytes()

    assert pkg._brief_scope(bundle, DS_UNIT, assets) == "model_only"
    assert pkg._brief_scope(bundle, UNIT, assets) == "report_only_shared_model"
    assert pkg._predicted_asset(bundle, DS_UNIT, assets) == provider
    assert pkg._predicted_asset(bundle, UNIT, assets) == consumer
    out = tmp_path / "out"
    for unit, asset, scope in (
        (DS_UNIT, provider, "model_only"),
        (UNIT, consumer, "report_only_shared_model"),
    ):
        brief = _brief(tmp_path, unit, scope, numeric_obligation="none")
        command = _package_command(bundle, oracle, out, unit, brief)
        if unit == UNIT:
            command.extend(["--provider-package", str(out / DS_UNIT)])
        assert pkg.main(command) == 0, f"canonical {scope} source must publish"
        package = out / unit
        manifest = json.loads((package / "package-manifest.json").read_bytes())
        assert manifest["construction_status"] == pkg.STATUS_ASSEMBLED
        assert manifest["artifacts"]["asset"] == f"assets/{asset.name}"
        assert (package / "assets" / asset.name).read_bytes() == asset.read_bytes()
        assert (package / "migration-brief.md").read_bytes() == brief.read_bytes()
        assert pkg.data_access.read_data_access(package / "data-access.json").effective_scope == scope
        assert pkg.pri.verify_s1(package).integrity.is_clean
    assert all(result.is_start_ready for result in pkg.pri.verify_phase1_role_identity([out / DS_UNIT, out / UNIT]))
    assert handover_path.read_bytes() == original_handover
    shipped_text = (out / UNIT / "handover" / f"{UNIT}.json").read_text(encoding="utf-8")
    shipped = json.loads(shipped_text)
    assert shipped["scope"] == {"unit": UNIT}
    assert "private-host-canary" not in shipped_text and private_path not in shipped["workbook"].values()
    assert json.dumps(str(assets))[1:-1] not in shipped_text
    if locator == "absolute":
        assert shipped["workbook"]["source_id"] != source_id
    else:
        assert shipped["workbook"]["source_id"] == source_id


def test_explicit_separate_provider_command_and_no_retroactive_or_sibling_rescue(tmp_path: Path) -> None:
    bundle, oracle = _shared_bundle(tmp_path)
    out = tmp_path / "out"
    command = _package_command(bundle, oracle, out, UNIT, _brief(tmp_path, UNIT, "report_only_shared_model"))
    assert pkg.main(command) == 0, "a non-self-contained diagnostic package is still ASSEMBLED"
    consumer = out / UNIT
    initial = (consumer / "data-access.json").read_bytes()
    assert pkg.data_access.read_data_access(consumer / "data-access.json").codes == ("provider-missing",)
    assert pkg.main(_package_command(bundle, oracle, out, DS_UNIT, _brief(tmp_path, DS_UNIT, "model_only"))) == 0
    provider = out / DS_UNIT
    assert pkg.data_access.read_data_access(provider / "data-access.json").state == "local_import_ready"
    assert (consumer / "data-access.json").read_bytes() == initial, "publishing a provider must not reopen consumers"
    assert pkg.main(command) == 0
    assert (consumer / "data-access.json").read_bytes() == initial, "a sibling is not an explicit provider"
    assert pkg.main([*command, "--provider-package", str(provider)]) == 0
    inherited = pkg.data_access.read_data_access(consumer / "data-access.json")
    assert (inherited.state, inherited.provider_state, inherited.validation, inherited.effective_scope) == (
        "provider_inherited",
        "local_import_ready",
        "validated",
        "report_only_shared_model",
    )
    assert inherited.source_keys == () and inherited.codes == ("provider-exact",)
    assert pkg.pri.verify_s1(consumer).integrity.is_clean
    assert all(result.is_start_ready for result in pkg.pri.verify_phase1_role_identity([provider, consumer]))
    record = json.loads((consumer / "package-manifest.json").read_text(encoding="utf-8"))
    assert record["self_contained"] is False and record["data_sources"]["self_contained"] is True
    assert str(provider) not in (consumer / "data-access.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("reverse", [False, True])
def test_provider_first_ordering_preserves_requested_denominator_and_final_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    bundle, oracle = _shared_bundle(tmp_path)
    out = tmp_path / "out"
    attempted = []
    assess = pkg._assess_package_data_access

    def observed(bundle_arg, candidate, local, **kwargs):
        manifest = json.loads((candidate / "package-manifest.json").read_text(encoding="utf-8"))
        attempted.append(manifest["unit"])
        assert pkg.data_access.read_data_access(candidate / "data-access.json").codes == ("projection-invalid",)
        assert pkg.pri.verify_s1(candidate).integrity.is_clean
        if manifest["unit"] == UNIT:
            assert kwargs["provider_packages"] == (out / DS_UNIT,)
            provider = out / DS_UNIT
            assert (provider / "README.md").is_file()
            assert pkg.pri.verify_s1(provider).integrity.is_clean
            assert not pkg.staging_dir(out, DS_UNIT).exists()
        return assess(bundle_arg, candidate, local, **kwargs)

    monkeypatch.setattr(pkg, "_assess_package_data_access", observed)
    selected = [DS_UNIT, UNIT] if reverse else [UNIT, DS_UNIT]
    json_path = tmp_path / "batch.json"
    args = ["--bundle", str(bundle), "--out", str(out), "--oracle", str(oracle), "--json", str(json_path), "--quiet"]
    for unit in selected:
        args.extend(["--unit", unit])
    assert pkg.main(args) == 0
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert attempted == [DS_UNIT, UNIT], "the alphabetical consumer must wait for the datasource's final swap"
    assert payload["requested"] == [UNIT, DS_UNIT]
    assert payload["totals"] == {
        "requested": 2,
        "units": 2,
        "failed": 0,
        "refused": 0,
        "unaccounted": 0,
        "assembled": 2,
        "blocked": 0,
    }
    assert [row["unit"] for row in payload["units"]] == [DS_UNIT, UNIT]
    assert [row["unit"] for row in payload["construction"]["assembled"]] == [DS_UNIT, UNIT]


@pytest.mark.parametrize("failure", ["assembly", "budget", "refused"])
def test_selected_failed_provider_never_reuses_even_an_explicit_stale_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bundle, oracle = _shared_bundle(tmp_path)
    out = tmp_path / "out"
    assert pkg.main(_package_command(bundle, oracle, out, DS_UNIT, _brief(tmp_path, DS_UNIT, "model_only"))) == 0
    provider = out / DS_UNIT
    prior = (provider / "package-manifest.json").read_bytes()
    observed = []
    assess = pkg._assess_package_data_access

    def no_stale(bundle_arg, candidate, local, **kwargs):
        observed.append(kwargs["provider_packages"])
        return assess(bundle_arg, candidate, local, **kwargs)

    monkeypatch.setattr(pkg, "_assess_package_data_access", no_stale)
    if failure == "budget":
        original = pkg.path_budget

        def budget(bundle_arg, unit, *args, **kwargs):
            if unit == DS_UNIT:
                raise pkg.PackagingError("fixture-budget-failed")
            return original(bundle_arg, unit, *args, **kwargs)

        monkeypatch.setattr(pkg, "path_budget", budget)
    else:
        original = pkg._assemble_unit

        def assembly(bundle_arg, unit, *args, **kwargs):
            if unit == DS_UNIT:
                if failure == "refused":
                    raise pkg.PackageEditsRefused(unit, provider, [], "fixture-refused")
                raise RuntimeError("fixture-assembly-failed")
            return original(bundle_arg, unit, *args, **kwargs)

        monkeypatch.setattr(pkg, "_assemble_unit", assembly)
    json_path = tmp_path / "batch.json"
    code = pkg.main(
        [
            "--bundle",
            str(bundle),
            "--out",
            str(out),
            "--oracle",
            str(oracle),
            "--provider-package",
            str(provider),
            "--json",
            str(json_path),
            "--quiet",
        ]
    )
    assert code == (3 if failure == "refused" else 5)
    assert observed == [()], "a failed/refused rebuild is not eligible through its old final root"
    assert (provider / "package-manifest.json").read_bytes() == prior
    assert pkg.pri.verify_s1(provider).integrity.is_clean
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["requested"] == [UNIT, DS_UNIT] and report["unaccounted"] == []
    assert [row["unit"] for row in report["construction"]["assembled"]] == [UNIT]
    assert [row["unit"] for row in report["construction"]["blocked"]] == [DS_UNIT]
    assert report["construction"]["blocked"][0]["reason_code"] == (
        "package_edits_refused"
        if failure == "refused"
        else ("unit_exception" if failure == "assembly" else "construction_failed")
    )


@pytest.mark.usefixtures("desktop")
@pytest.mark.parametrize("selection", ["default-engine", "explicit-directory", "explicit-spec", "sibling-only"])
def test_real_packaging_binds_only_the_exact_original_gate_root(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None, datasource_only=True)
    asset = bundle.parent / "assets" / f"{DS_LUID}_{DS_UNIT}.tds"
    asset.write_text(
        "<datasource name='Live'><connection class='sqlserver' server='source.example' dbname='db'/></datasource>",
        encoding="utf-8",
    )
    _write_input_manifest(bundle, list(asset.parent.glob("*")))
    report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
    report["datasources"][0]["connection"] = dict(authority.LIVE)
    s2._write(bundle / "report.json", report)
    authority._earn(bundle if selection == "default-engine" else root)
    observed = []
    assess = pkg.data_access.assess_data_access

    def exact_gate(gate_root, **kwargs):
        observed.append(gate_root)
        return assess(gate_root, **kwargs)

    monkeypatch.setattr(pkg.data_access, "assess_data_access", exact_gate)
    options = (
        {}
        if selection in ("default-engine", "sibling-only")
        else {"gate_root": root if selection == "explicit-directory" else root / "migration-spec.json"}
    )
    brief = _brief(tmp_path, DS_UNIT, "model_only")
    if selection == "explicit-spec":
        command = _package_command(bundle, oracle, tmp_path / "out", DS_UNIT, brief)
        assert pkg.main([*command, "--gate-root", str(root / "migration-spec.json")]) == 0
    else:
        pkg.package_unit(
            bundle, DS_UNIT, tmp_path / "out", oracle_dir=oracle, assets_dir=asset.parent, brief=brief, **options
        )
    package = tmp_path / "out" / DS_UNIT
    result = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    actual = pkg.data_access.read_data_access(package / "data-access.json")
    assert (actual.state, actual.codes) == (
        ("cannot_establish", ("audit-missing",))
        if selection == "sibling-only"
        else ("live_data_ok", ("probe-cleared", "probe-data-ok"))
    )
    assert observed == [bundle if selection in ("default-engine", "sibling-only") else root]
    assert not any(path.name.startswith(".credential-gate") for path in package.rglob("*"))
    assert not any(key in result for key in ("gate_root", "provider_packages"))
    assert "source.example" not in (package / "data-access.json").read_text(encoding="utf-8")


def test_malformed_gate_adapter_input_remains_a_valid_nonaccepted_projection(tmp_path: Path) -> None:
    root = tmp_path / "original"
    root.mkdir()
    s2._write(root / "migration-spec.json", [])
    candidate = _assessment_candidate(tmp_path, authority._spec(authority.LIVE))
    result = _assess_candidate(candidate, root)
    assert (result.state, result.codes) == ("cannot_establish", ("projection-invalid",))


@pytest.mark.parametrize("change", ["tamper", "delete", "extra", "declaration-only"])
def test_s1_hashes_projection_bytes_but_does_not_interpret_its_declaration(tmp_path: Path, change: str) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    result = pkg.package_unit(
        bundle,
        UNIT,
        tmp_path / "out",
        oracle_dir=oracle,
        assets_dir=bundle.parent / "assets",
        brief=_brief(tmp_path, UNIT),
    )
    package = tmp_path / "out" / UNIT
    path = package / "data-access.json"
    assert result["contents"]["files"]["data-access.json"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["artifacts"]["data_access"] == "data-access.json"
    if change == "tamper":
        path.write_bytes(path.read_bytes() + b" ")
    elif change == "delete":
        path.unlink()
    elif change == "extra":
        (package / "extra-data-access.json").write_bytes(path.read_bytes())
    else:
        manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
        manifest["artifacts"].pop("data_access")
        s2._write(package / "package-manifest.json", manifest)
    result_s1 = pkg.pri.verify_s1(package).integrity
    assert (
        result_s1.codes()
        == {
            "tamper": ("package_file_digest_mismatch",),
            "delete": ("package_file_missing",),
            "extra": ("package_file_undeclared",),
            "declaration-only": (),
        }[change]
    ), "S1 remains byte authority; the final consumer must require the role declaration"


@pytest.mark.parametrize(
    "fault", ["unknown", "missing", "duplicate", "nonfinite", "codes", "keys", "provider", "scope"]
)
def test_strict_serialization_refuses_unsafe_or_lossy_authority_output(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    candidate = _assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    canary = "private-host-source-credential-canary"
    payload = {**LOCAL_PROJECTION, "effective_scope": "model_and_report"}
    if fault == "unknown":
        payload[canary] = "unsafe"
    elif fault == "missing":
        payload.pop("validation")
    elif fault == "codes":
        payload["codes"] = list(reversed(payload["codes"]))
    elif fault == "keys":
        payload.update(
            state="live_data_ok", source_keys=[authority.KEY, authority.KEY], codes=["probe-cleared", "probe-data-ok"]
        )
    elif fault == "provider":
        payload.update(
            state="provider_inherited",
            provider_unit=canary,
            provider_state="local_import_ready",
            codes=["provider-exact"],
        )
    elif fault == "scope":
        payload["effective_scope"] = "all"
    wire = json.dumps(payload)
    if fault == "duplicate":
        wire = wire[:-1] + ',"state":"blocked"}'
    elif fault == "nonfinite":
        wire = wire.replace('"validation": "validated"', '"validation": NaN')
    monkeypatch.setattr(
        pkg.data_access, "assess_data_access", lambda *_args, **_kwargs: SimpleNamespace(dumps=lambda: wire)
    )
    result, notes = pkg._assess_package_data_access(
        root, candidate, copy.deepcopy(authority.LOCAL), gate_root=root, provider_packages=()
    )
    assert (result.state, result.codes) == ("cannot_establish", ("projection-invalid",))
    assert canary not in result.dumps() + json.dumps(notes)
    assert set(result.to_json()) == {
        "schema",
        "state",
        "source_keys",
        "provider_unit",
        "provider_state",
        "validation",
        "effective_scope",
        "max_phase2_claim",
        "codes",
    }


@pytest.mark.parametrize("error", [ValueError, OSError, TypeError])
def test_new_diagnostics_never_echo_assessment_exception_content(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    candidate = _assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    canary = "private-source-credential-provider-canary"

    def cannot_read(*_args, **_kwargs):
        raise error(canary)

    monkeypatch.setattr(pkg.data_access, "assess_data_access", cannot_read)
    result, notes = pkg._assess_package_data_access(
        root, candidate, copy.deepcopy(authority.LOCAL), gate_root=root, provider_packages=()
    )
    assert (result.state, result.codes) == ("cannot_establish", ("projection-invalid",))
    assert canary not in result.dumps() + json.dumps(notes)


def test_projection_allowlist_refuses_drops_and_does_not_echo_untrusted_keys(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import manifest_scope as scope

    assert set(scope.DATA_ACCESS_ALLOW) == set(LOCAL_PROJECTION)
    assert scope.project_data_access(LOCAL_PROJECTION) == LOCAL_PROJECTION
    for payload in (
        {**LOCAL_PROJECTION, "private-canary": "unsafe"},
        {**LOCAL_PROJECTION, "state": {"private-canary": "unsafe"}},
    ):
        with pytest.raises(scope.UnscopedStructure, match="^data-access projection refused$") as caught:
            scope.project_data_access(payload)
        assert "private-canary" not in str(caught.value)
    candidate = _assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    monkeypatch.setattr(pkg, "project_data_access", lambda payload: {**payload, "codes": []})
    result = _assess_candidate(candidate, root)
    assert (result.state, result.codes) == ("cannot_establish", ("projection-invalid",)), (
        "zero-drop equality is mandatory"
    )


@pytest.mark.parametrize("failure", ["after-provisional", "edit-during-assessment"])
def test_publication_failure_or_edit_cannot_replace_a_prior_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    out = tmp_path / "out"
    options = {"oracle_dir": oracle, "assets_dir": bundle.parent / "assets", "brief": _brief(tmp_path, UNIT)}
    pkg.package_unit(bundle, UNIT, out, **options)
    final = out / UNIT
    previous = {path.relative_to(final).as_posix(): path.read_bytes() for path in final.rglob("*") if path.is_file()}
    assess = pkg._assess_package_data_access

    def intervene(bundle_arg, candidate, local, **kwargs):
        assert pkg.pri.verify_s1(candidate).integrity.is_clean
        assert pkg.data_access.read_data_access(candidate / "data-access.json").codes == ("projection-invalid",)
        if failure == "after-provisional":
            raise RuntimeError("fixture-after-provisional")
        (final / "handover.md").write_bytes(b"operator edit survives")
        return assess(bundle_arg, candidate, local, **kwargs)

    monkeypatch.setattr(pkg, "_assess_package_data_access", intervene)
    expected = RuntimeError if failure == "after-provisional" else pkg.PackageEditsRefused
    with pytest.raises(expected) as caught:
        pkg.package_unit(bundle, UNIT, out, **options)
    if failure == "after-provisional":
        assert str(caught.value) == "fixture-after-provisional"
    else:
        assert caught.value.changed == ["handover.md"]
        previous["handover.md"] = b"operator edit survives"
    assert {
        path.relative_to(final).as_posix(): path.read_bytes() for path in final.rglob("*") if path.is_file()
    } == previous
    assert not pkg.staging_dir(out, UNIT).exists()


def test_final_manifest_seals_final_projection_handover_and_brief_and_accounts_for_path_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    out = tmp_path / "out"
    paths = pkg.projected_paths(bundle, UNIT, out, assets_dir=bundle.parent / "assets")
    projection_paths = [path for path in paths if path.tail == "data-access.json"]
    assert len(projection_paths) == 3, "final, staging and retired roots all need the scaffold's cost"
    seals = []
    write = pkg.write_json

    def manifest_written_last(path, payload):
        write(path, payload)
        if path.name == "package-manifest.json":
            for name in ("data-access.json", "migration-brief.md", "handover.md", "README.md"):
                if name in payload["contents"]["files"]:
                    assert (
                        payload["contents"]["files"][name]
                        == hashlib.sha256((path.parent / name).read_bytes()).hexdigest()
                    )
            seals.append(pkg.data_access.read_data_access(path.parent / "data-access.json").state)

    monkeypatch.setattr(pkg, "write_json", manifest_written_last)
    pkg.package_unit(
        bundle, UNIT, out, oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=_brief(tmp_path, UNIT)
    )
    assert seals == ["cannot_establish", "local_import_ready"]
    assert pkg.pri.verify_s1(out / UNIT).integrity.is_clean


def test_final_s1_refuses_digest_drift_after_the_last_manifest_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    out = tmp_path / "out"
    options = {"oracle_dir": oracle, "assets_dir": bundle.parent / "assets", "brief": _brief(tmp_path, UNIT)}
    pkg.package_unit(bundle, UNIT, out, **options)
    prior = (out / UNIT / "package-manifest.json").read_bytes()
    write = pkg.write_json
    seals = []

    def corrupt_final_projection(path, payload):
        write(path, payload)
        if path.name == "package-manifest.json":
            seals.append(True)
            if len(seals) == 2:
                data_file = path.parent / "data-access.json"
                data_file.write_bytes(data_file.read_bytes() + b" ")
                assert pkg.pri.verify_s1(path.parent).integrity.codes() == ("package_file_digest_mismatch",)

    monkeypatch.setattr(pkg, "write_json", corrupt_final_projection)
    with pytest.raises(pkg.PackagingError, match="^data_access_final_integrity_failed$"):
        pkg.package_unit(bundle, UNIT, out, **options)
    assert len(seals) == 2
    assert (out / UNIT / "package-manifest.json").read_bytes() == prior
    assert not pkg.staging_dir(out, UNIT).exists()


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('schema = "phase1-start-ready/v1"', 'schema = "unknown"'),
        ('fallback_authorization = "stop"', "fallback_authorization = true"),
        ('fallback_authorization = "stop"', 'fallback_authorization = "stop"\nextra = "private-canary"'),
    ],
)
def test_invalid_explicit_policy_is_refused_before_any_assembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    brief = _brief(tmp_path, UNIT)
    brief.write_text(brief.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
    calls = []
    monkeypatch.setattr(pkg, "_assemble_unit", lambda *_args, **_kwargs: calls.append(True))
    with pytest.raises(pkg.PackagingError, match="^brief_policy_invalid$") as caught:
        pkg.package_unit(
            bundle, UNIT, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=brief
        )
    assert not calls and not (tmp_path / "out").exists()
    assert "private-canary" not in str(caught.value)


def test_missing_staged_spec_publishes_cannot_establish_not_an_absent_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    monkeypatch.setattr(pkg, "_write_spec", lambda *_args: (None, "fixture-spec-unavailable"))
    result = pkg.package_unit(
        bundle,
        UNIT,
        tmp_path / "out",
        oracle_dir=oracle,
        assets_dir=bundle.parent / "assets",
        brief=_brief(tmp_path, UNIT),
    )
    final = tmp_path / "out" / UNIT
    assert result["artifacts"]["migration_spec"] is None
    actual = pkg.data_access.read_data_access(final / "data-access.json")
    assert (actual.state, actual.codes) == ("cannot_establish", ("spec-unreadable",))
    assert result["artifacts"]["data_access"] == "data-access.json"
    assert pkg.pri.verify_s1(final).integrity.is_clean
