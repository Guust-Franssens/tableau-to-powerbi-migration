"""Producer-side controls for the bounded S2 correction: source row binding and brief containment."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

import test_data_access_contract as authority
import test_package_role_identity as s2
from test_data_access_contract import _desktop_fixture, _root_fixture  # noqa: F401  # existing pytest fixtures
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
    assert result["packaged"] is True and result["self_contained"] is True
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
        assert kwargs["package_data_sources"] is localized[0]
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
    assert result["data_sources"] is localized[0]
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


def _direct_provider(parent: Path, *, luid: str = DS_LUID, key: str = authority.KEY) -> Path:
    package = s2.datasource_package(parent, unit="Shared", luid=luid)
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
    return s2.workbook_package(package, published={"luid": DS_LUID}, binding=binding.replace("\\", "/"))


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("repeated", [False, True])
def test_inheritance_uses_exact_ordinal_not_duplicate_provider_unit_names(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool, repeated: bool
) -> None:
    selected = _direct_provider(tmp_path / "selected", key="source-key:ab1baa4b3f77bb70")
    other = _direct_provider(tmp_path / "other", luid=s2.WB_LUID, key="source-key:e625ce798a6d19bb")
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
    elif fault == "repeated-invalid":
        spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
        spec["data_sources"].append({"published_datasource": None})
        s2._write(consumer / "migration-spec.json", spec)
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


def test_mixed_published_and_direct_consumers_refuse_the_authoritys_inheritance_shortcut(
    tmp_path: Path, root: Path
) -> None:
    provider = _direct_provider(tmp_path / "provider")
    consumer = _provider_consumer(tmp_path, provider)
    assert _assess_candidate(consumer, root, providers=(provider,)).state == "provider_inherited"
    spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
    spec["data_sources"].extend(authority._spec(authority.LIVE)["data_sources"])
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
    other = _direct_provider(tmp_path / "other", luid=s2.WB_LUID, key=authority.OTHER_KEY)
    consumer = _provider_consumer(tmp_path, selected)
    spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
    spec["data_sources"].append({"published_datasource": {"luid": s2.WB_LUID}})
    s2._write(consumer / "migration-spec.json", spec)
    _reseal(consumer)
    verdict = pkg.pri.verify_phase1_role_identity([selected, other, consumer])[-1]
    assert [dep.state for dep in verdict.dependencies] == ["resolved", "mismatch"]
    assert verdict.dependencies[1].code == "provider_binding_mismatch"
    result = _assess_candidate(consumer, root, providers=(selected, other))
    assert (result.state, result.codes) == ("cannot_establish", ("provider-foreign",))
