"""Producer-side controls for the bounded S2 correction: source row binding and brief containment."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from test_package_unit_gates import DS_LUID, DS_UNIT, UNIT, _brief, _bundle, pkg


def selected_input(root: Path) -> tuple[Path, Path, dict[str, Any]]:
    """One independently declared datasource row and its source bytes."""
    bundle = root / "bundle"
    bundle.mkdir(parents=True)
    asset = root / "assets" / f"{DS_LUID}_{DS_UNIT}.tds"
    asset.parent.mkdir()
    asset.write_text("<datasource/>", encoding="utf-8")
    row = {
        "name": asset.name,
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


def test_staged_input_path_cannot_change_the_selecting_rows_basename(tmp_path: Path) -> None:
    bundle, asset, row = selected_input(tmp_path)
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

    with pytest.raises(pkg.PackagingError, match="^source_asset_row_ambiguous$"):
        pkg.resolve_asset(bundle, DS_UNIT, {}, asset.parent)


def test_datasource_candidates_never_choose_the_first_directory(tmp_path: Path) -> None:
    bundle, asset, _row = selected_input(tmp_path)
    second = bundle / "assets" / asset.name
    second.parent.mkdir()
    shutil.copyfile(asset, second)

    with pytest.raises(pkg.PackagingError, match="^source_asset_candidate_ambiguous$"):
        pkg.resolve_asset(bundle, DS_UNIT, {}, asset.parent)


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


def test_single_brief_is_refused_for_a_multi_unit_command_before_writing(tmp_path: Path) -> None:
    bundle, _oracle, _objects = _bundle(tmp_path, covered=None, datasource_only=True)
    brief = _brief(tmp_path, UNIT)
    out = tmp_path / "packages"

    with pytest.raises(SystemExit) as refused:
        pkg.main(["--bundle", str(bundle), "--out", str(out), "--brief", str(brief), "--quiet"])

    assert refused.value.code == 2
    assert not out.exists(), "a single typed brief must not be broadcast into any package"


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


def test_brief_copy_uses_the_same_bytes_that_passed_preassembly_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, oracle, _objects = _bundle(tmp_path, covered=None)
    brief = _brief(tmp_path, UNIT)
    expected = brief.read_bytes()
    original = pkg._assemble_unit

    def changed_after_validation(*args, **kwargs):
        brief.write_text("X-Tableau-Auth: a-new-private-token", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(pkg, "_assemble_unit", changed_after_validation)
    pkg.package_unit(
        bundle, UNIT, tmp_path / "out", oracle_dir=oracle, assets_dir=bundle.parent / "assets", brief=brief
    )

    assert (tmp_path / "out" / UNIT / "migration-brief.md").read_bytes() == expected
