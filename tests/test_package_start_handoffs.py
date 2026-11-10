"""Direct #562 prerequisite controls, not an implementation of the final START_READY consumer.

Hand-built S1/S2 fixtures and literal projection/key oracles separate bytes, binding and applicability.
No audit, credential change, probe, source search, provider permission or final readiness fold here.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import os
import shutil
import stat
from dataclasses import fields, replace
from pathlib import Path

import pytest

from test_package_filesystem import build_package, classify, link_directory, verify, write_manifest
from test_package_role_identity import (
    DS_LUID,
    PUBLISHED_KEY,
    datasource_package,
    pri,
    seal,
)


import package_data_access as pda
import package_filesystem as pfs

# pylint: disable=protected-access,missing-function-docstring

SPEC = "migration-spec.json"
PROJECTION = "data-access.json"
LIVE = {
    "class": "sqlserver",
    "server": "source.example",
    "database": "db",
    "schema": "dbo",
    "name": "orders-leg",
    "powerbi_target": "live_source",
}
OTHER = {**LIVE, "server": "other.example"}
FLAT = {"class": "excel-direct", "powerbi_target": "flat_file"}
REVIEW = {"class": "unknown", "server": "review.example", "powerbi_target": "unknown"}


def ordinary_key(server: str) -> str:
    """Independent SHA-256 oracle for the documented dbo.Orders identity."""
    identity = {
        "class": "sqlserver",
        "server": server,
        "database": "db",
        "schema": "dbo",
        "ordinary_tables": ["Orders"],
    }
    return (
        "source-key:"
        + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    )


LIVE_KEY = ordinary_key("source.example")
OTHER_KEY = ordinary_key("other.example")
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
LIVE_PROJECTION = {
    **LOCAL_PROJECTION,
    "state": "live_data_ok",
    "source_keys": [LIVE_KEY],
    "codes": ["probe-cleared", "probe-data-ok"],
}
MODEL_ONLY_PROJECTION = {
    **LIVE_PROJECTION,
    "state": "authorized_model_only",
    "validation": "unvalidated",
    "max_phase2_claim": "structural_only",
    "codes": ["brief-model-only", "human-authorize"],
}
PROVIDER_REF = "provider-ref:v1:sha256:c012985255180d7070f985ab17164c4c9975f518671f289d81cdcac409ece270"
INHERITED_PROJECTION = {
    **LIVE_PROJECTION,
    "state": "provider_inherited",
    "provider_unit": PROVIDER_REF,
    "provider_state": "live_data_ok",
    "effective_scope": "report_only_shared_model",
    "codes": ["provider-exact"],
}
PUBLISHED = {
    "id": "ds",
    "connection": {"class": "sqlproxy", "mode": "live", "powerbi_target": "flat_file"},
    "published_datasource": {"luid": DS_LUID, "key": PUBLISHED_KEY},
}


def source_rows(*connections: dict) -> list[dict]:
    """A minimal spec row, retaining real canonical connection classification."""
    return [
        {
            "connection": dict(connection),
            "tables": [
                {"name": "Orders", "table": "[dbo].[Orders]", "connection": connection.get("name", "orders-leg")}
            ],
            "fields": [],
        }
        for connection in connections
    ]


def with_data_access(package: Path, rows: list | None = None, projection: dict | bytes | None = None) -> Path:
    """Declare exact role bytes without invoking the producer or authority under test."""
    spec = json.loads((package / SPEC).read_bytes())
    if rows is not None:
        spec["data_sources"] = copy.deepcopy(rows)
    (package / SPEC).write_bytes(json.dumps(spec, ensure_ascii=False).encode("utf-8"))
    raw = projection if isinstance(projection, bytes) else json.dumps(projection or LOCAL_PROJECTION).encode("utf-8")
    (package / PROJECTION).write_bytes(raw)
    manifest = json.loads((package / "package-manifest.json").read_bytes())
    manifest["artifacts"]["data_access"] = PROJECTION
    return seal(package, **manifest)


def filesystem_package(root: Path) -> Path:
    package = build_package(root, files={SPEC: "", PROJECTION: ""})
    (package / SPEC).write_bytes(b'{"data_sources":[]}\r\n')
    (package / PROJECTION).write_bytes(b'{"held":true}\r\n')
    write_manifest(package)
    return package


def require_held(value: object) -> pfs.HeldVerifiedMember:
    assert isinstance(value, pfs.HeldVerifiedMember), value
    return value


def require_handoff(value: object) -> pri.PackageDataAccessHandoff:
    assert isinstance(value, pri.PackageDataAccessHandoff), value
    return value


def assert_refusal(value: object, code: str) -> None:
    assert isinstance(value, pfs.PackageFilesystemResult), f"expected typed S1 refusal {code}, got {value!r}"
    assert value.first_code == code
    assert not value.is_clean
    assert all(row.path is None for row in (*value.findings, *value.unassessable))


def test_s1_holds_no_large_asset_bytes_and_reader_does_not_parse_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = filesystem_package(tmp_path)
    asset = package / "large.bin"
    with asset.open("wb") as handle:
        for _index in range(3):
            handle.write(b"asset" * (1 << 18))
    write_manifest(package)
    classification = classify(package)
    original = Path.read_bytes
    opened = []

    def read_once(path: Path) -> bytes:
        assert path != asset, "large assets must be streamed, not held in the S1 result"
        opened.append(path.name)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_once)
    verified = pfs.verify_package(package, classification)
    assert verified.is_clean
    assert opened == ["package-manifest.json"]
    assert all(
        not isinstance(getattr(member, field.name), bytes)
        for member in verified.verified_files
        for field in fields(member)
    )
    assert len(verified.manifest.content) < 4096

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a held member reader reparsed or rediscovered the manifest authority")

    monkeypatch.setattr(pfs, "parse_manifest_text", forbidden)
    monkeypatch.setattr(pfs, "declared_files", forbidden)
    member = require_held(pfs.read_verified_member(package, verified, PROJECTION))
    assert member.content == b'{"held":true}\r\n'
    assert opened == ["package-manifest.json", PROJECTION]


@pytest.mark.parametrize(
    "change,code",
    [
        ("deleted", "package_file_missing"),
        ("added", "package_file_undeclared"),
        ("manifest-digest", "package_file_digest_mismatch"),
        ("same-byte-replacement", "package_member_replaced"),
        ("hardlink", "package_member_replaced"),
        ("directory", "package_file_missing"),
        ("case-alias", "package_file_undeclared"),
    ],
)
def test_held_read_refuses_changed_namespace_or_identity(tmp_path: Path, change: str, code: str) -> None:
    package = filesystem_package(tmp_path)
    verified = verify(package)
    path = package / PROJECTION
    if change == "deleted":
        path.unlink()
    elif change == "added":
        (package / "extra.bin").write_bytes(b"new")
    elif change == "manifest-digest":
        target = package / "package-manifest.json"
        target.write_bytes(target.read_bytes() + b"\n")
    elif change == "same-byte-replacement":
        raw = path.read_bytes()
        path.rename(tmp_path / "retired.bin")
        path.write_bytes(raw)
    elif change == "hardlink":
        os.link(path, tmp_path / "alias.bin")
    elif change == "directory":
        path.unlink()
        path.mkdir()
    else:
        path.rename(package / "renaming")
        (package / "renaming").rename(package / PROJECTION.upper())
    assert_refusal(pfs.read_verified_member(package, verified, PROJECTION), code)


@pytest.mark.parametrize("junction", [False, True])
def test_same_byte_replaced_root_never_reuses_s1(tmp_path: Path, junction: bool) -> None:
    package = filesystem_package(tmp_path)
    verified = verify(package)
    retired = package.rename(tmp_path / "retired")
    if junction:
        link_directory(package, retired)
    else:
        shutil.copytree(retired, package)
    try:
        assert_refusal(pfs.read_verified_member(package, verified, PROJECTION), "package_root_replaced")
    finally:
        if junction:
            if os.name == "nt":
                package.rmdir()
            else:
                package.unlink()


def test_s1_root_identity_is_required_even_for_identical_bytes(tmp_path: Path) -> None:
    package = filesystem_package(tmp_path)
    verified = verify(package)
    assert require_held(pfs.read_verified_member(package, verified, PROJECTION)).root_identity == str(package)
    foreign = replace(verified, root_identity=str(package).swapcase())
    assert_refusal(pfs.read_verified_member(package, foreign, PROJECTION), "package_root_binding_invalid")


@pytest.mark.parametrize("name", ["data-access.json."])
def test_only_exact_canonical_member_spelling_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str | None
) -> None:
    package = filesystem_package(tmp_path)
    verified = verify(package)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an unverified spelling reached the filesystem")

    monkeypatch.setattr(pfs, "walk_package", forbidden)
    assert_refusal(pfs.read_verified_member(package, verified, name), "package_member_not_verified")


@pytest.mark.parametrize(
    "mode,code", [(stat.S_IFLNK, "package_entry_reparse"), (stat.S_IFCHR, "package_entry_not_regular")]
)
def test_reparse_and_nonregular_members_refuse_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int, code: str
) -> None:
    package = filesystem_package(tmp_path)
    verified = verify(package)
    original = os.DirEntry.stat

    def changed(entry: os.DirEntry, *, follow_symlinks: bool = True) -> os.stat_result:
        info = original(entry, follow_symlinks=follow_symlinks)
        if entry.name != PROJECTION:
            return info
        values = list(info)
        values[0] = (info.st_mode & ~stat.S_IFMT(info.st_mode)) | mode
        extras = {key: getattr(info, key) for key in ("st_file_attributes", "st_reparse_tag") if hasattr(info, key)}
        return os.stat_result(values, extras)

    def forbidden(_path: Path) -> bytes:
        pytest.fail("a reparse/nonregular member was read")

    monkeypatch.setattr(os.DirEntry, "stat", changed)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert_refusal(pfs.read_verified_member(package, verified, PROJECTION), code)


def test_unreadable_member_returns_only_fixed_s1_diagnostics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = filesystem_package(tmp_path)
    verified = verify(package)

    def denied(_path: Path) -> bytes:
        raise PermissionError("PRIVATE endpoint source-name audit path credential")

    monkeypatch.setattr(Path, "read_bytes", denied)
    result = pfs.read_verified_member(package, verified, PROJECTION)
    assert_refusal(result, "package_file_unreadable")
    assert result.status == "unassessable"
    serialized = json.dumps(result.as_dict())
    assert "PRIVATE" not in serialized and str(package) not in serialized and '"path"' not in serialized


def test_s2_spec_is_parsed_once_and_only_the_authority_walks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = with_data_access(datasource_package(tmp_path / "Source", luid=None), source_rows(LIVE), LIVE_PROJECTION)
    original_read, original_walk, derive = Path.read_bytes, pfs._walk_package, pri.package_spec_facts
    reads, inputs = [], []

    def recorded(path: Path) -> bytes:
        if path.name == SPEC:
            reads.append(path)
        return original_read(path)

    def walked(root: Path):
        assert inspect.currentframe().f_back.f_globals["__name__"] == "package_filesystem", "consumer rewalked S1"
        return original_walk(root)

    def facts(spec: object) -> pda.PackageSpecFacts:
        inputs.append(copy.deepcopy(spec))
        return derive(spec)

    monkeypatch.setattr(Path, "read_bytes", recorded)
    monkeypatch.setattr(pfs, "_walk_package", walked)
    monkeypatch.setattr(pri, "package_spec_facts", facts)
    result = pri.verify_phase1_role_identity([package])[0]
    assert result.is_start_ready, result.codes()
    assert reads == [package / SPEC] and len(inputs) == 1
    assert inputs[0]["data_sources"] == source_rows(LIVE)
    handoff = require_handoff(result.data_access_handoff(package))
    assert reads == [package / SPEC, package / SPEC] and len(inputs) == 1, (
        "freshness recheck reparsed/reclassified the spec"
    )
    assert handoff.facts.live_source_keys == (LIVE_KEY,)


@pytest.mark.parametrize("change", ["target", "case", "verified", "snapshot"])
def test_s2_snapshot_cannot_borrow_another_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    first = with_data_access(datasource_package(tmp_path / "First", luid=None), source_rows(LIVE), LIVE_PROJECTION)
    second = with_data_access(datasource_package(tmp_path / "Second", luid=None), source_rows(FLAT))
    result, other = pri.verify_phase1_role_identity([first, second])
    target = first
    if change == "target":
        target = second
    elif change == "case":
        target = Path(str(first).swapcase())
    elif change == "verified":
        result = replace(result, verified=other.verified)
    else:
        result = replace(result, _data_access_snapshot=other._data_access_snapshot)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a cross-root S2 handoff reached an S1 read")

    monkeypatch.setattr(pfs, "read_verified_member", forbidden)
    assert_refusal(result.data_access_handoff(target), "package_root_binding_invalid")


@pytest.mark.parametrize("name", [SPEC, PROJECTION])
def test_post_s2_changes_refuse_instead_of_mixing_snapshots(tmp_path: Path, name: str) -> None:
    package = with_data_access(datasource_package(tmp_path / "Source"), source_rows(LIVE), LIVE_PROJECTION)
    result = pri.verify_phase1_role_identity([package])[0]
    path = package / name
    path.write_bytes(path.read_bytes() + b"\n")
    assert_refusal(result.data_access_handoff(package), "package_file_digest_mismatch")


@pytest.mark.parametrize(
    "connections,keys,review,refusal",
    [
        ([LIVE, LIVE], (), False, "source-key-invalid"),
        ([{"class": "sqlserver", "powerbi_target": "live_source"}], (), False, "source-key-invalid"),
        ([{**LIVE, "connections": "malformed"}], (), False, "spec-unreadable"),
    ],
)
def test_pure_spec_facts_reuse_canonical_leg_derivation(
    connections: list[dict], keys: tuple[str, ...], review: bool, refusal: str | None
) -> None:
    spec = {"data_sources": source_rows(*connections)}
    original = copy.deepcopy(spec)
    facts = pda.package_spec_facts(spec)
    assert (facts.live_source_keys, facts.has_review, facts.refusal_code) == (keys, review, refusal)
    assert facts.all_flat is (connections == [FLAT])
    assert not facts.published_only
    assert spec == original
    with pytest.raises(AttributeError):
        facts.has_review = False


@pytest.mark.parametrize("rows", [None, [None], [{"connection": []}], [{"fields": False}]])
def test_malformed_source_sets_preserve_authority_refusal(rows: object) -> None:
    facts = pda.package_spec_facts({"data_sources": rows})
    assert facts.refusal_code == "spec-unreadable"
    assert facts.live_source_keys == () and not facts.all_flat and not facts.published_only


@pytest.mark.parametrize(
    "extra",
    [
        {"connection": {**PUBLISHED["connection"], "mode": True}},
        {"fields": [{"nested": {"connections": [LIVE]}}]},
        {"fields": False},
        {"caption": []},
    ],
)
def test_malformed_or_nested_published_rows_never_gain_clean_applicability(extra: dict) -> None:
    facts = pda.package_spec_facts({"data_sources": [{**PUBLISHED, **extra}]})
    assert not facts.published_only and not facts.direct_applicable and not facts.all_flat


def test_new_handoffs_add_no_serialized_private_diagnostics(tmp_path: Path) -> None:
    private = {**LIVE, "server": "private-endpoint.example", "database": "private-database"}
    package = with_data_access(
        datasource_package(tmp_path / "PrivateRoot", luid=None), source_rows(private), LIVE_PROJECTION
    )
    result = pri.verify_phase1_role_identity([package])[0]
    handoff = require_handoff(result.data_access_handoff(package))
    assert set(result.as_dict()) == {
        "verdict",
        "unit",
        "kind",
        "topology",
        "roles",
        "source_identity",
        "dependencies",
        "blockers",
        "authorized_limitations",
    }
    serialized = json.dumps(result.as_dict()) + json.dumps(result.verified.integrity.as_dict()) + repr(handoff)
    for forbidden in (
        "private-endpoint",
        "private-database",
        "PrivateRoot",
        "data_sources",
        ".credential-gate",
    ):
        assert forbidden not in serialized
    assert str(tmp_path) not in serialized and repr(handoff.migration_spec.content) not in serialized


def rebuild_result(value: object) -> object:
    """Reconstruct every constructor field, without assuming knowledge of a private capability."""
    return type(value)(**{field.name: getattr(value, field.name) for field in fields(value) if field.init})


def test_r1_s1_rejects_contradictory_original_state(tmp_path: Path) -> None:
    package = filesystem_package(tmp_path)
    original = verify(package)
    assert require_held(pfs.read_verified_member(package, original, PROJECTION)).relative_path == PROJECTION
    object.__setattr__(original, "files_verified", 0)
    result = pfs.read_verified_member(package, original, PROJECTION)
    assert isinstance(result, pfs.PackageFilesystemResult), "contradictory original S1 state minted held bytes"
    assert result.first_code == "package_member_not_verified"


@pytest.mark.parametrize("field", ["content", "facts", "blockers", "role", "snapshot"])
def test_r1_s2_checks_original_handoff_consistency(tmp_path: Path, field: str) -> None:
    """Change one field in place to exercise consistency separately from copied-result rejection."""
    package = with_data_access(datasource_package(tmp_path / "Source"), source_rows(LIVE), LIVE_PROJECTION)
    original = pri.verify_phase1_role_identity([package])[0]
    require_handoff(original.data_access_handoff(package))
    snapshot = original._data_access_snapshot
    if field == "content":
        object.__setattr__(snapshot.spec, "content", b'{"R1_PRIVATE_SPEC":"forged"}')
    elif field == "facts":
        object.__setattr__(snapshot, "facts", snapshot.facts._replace(live_source_keys=()))
    elif field == "blockers":
        object.__setattr__(original, "blockers", ("R1_PRIVATE_BLOCKER",))
    elif field == "role":
        role = next(role for role in original.roles if role.role == pri.ROLE_MIGRATION_SPEC)
        object.__setattr__(role, "state", "missing")
    else:
        object.__setattr__(original, "_data_access_snapshot", rebuild_result(snapshot))
    result = original.data_access_handoff(package)
    assert isinstance(result, pfs.PackageFilesystemResult), (
        f"S2 original {field} inconsistency returned forged authority"
    )
    if field == "content":
        assert result.first_code == "package_file_digest_mismatch"
    assert "R1_PRIVATE" not in json.dumps(result.as_dict())


@pytest.mark.parametrize("surface", ["s1", "s2"])
def test_r1_small_metadata_never_rehashes_unrelated_eight_mib_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: str
) -> None:
    package = with_data_access(datasource_package(tmp_path / "Source"), source_rows(FLAT))
    unrelated = package / "data" / "large.bin"
    unrelated.parent.mkdir()
    unrelated.write_bytes(b"Z" * (8 << 20))
    seal(package, **json.loads((package / "package-manifest.json").read_bytes()))
    role_result = pri.verify_phase1_role_identity([package])[0]
    assert role_result.is_start_ready
    original_hash, original_read = pfs._hash_file, Path.read_bytes
    hashed, held = [], []

    def tracked_hash(path: Path) -> str | None:
        hashed.append(path)
        return original_hash(path)

    def tracked_read(path: Path) -> bytes:
        held.append(path)
        return original_read(path)

    monkeypatch.setattr(pfs, "_hash_file", tracked_hash)
    monkeypatch.setattr(Path, "read_bytes", tracked_read)
    if surface == "s1":
        for name in (SPEC, PROJECTION):
            require_held(role_result.verified.read_verified_member(package, name))
    else:
        require_handoff(role_result.data_access_handoff(package))
    assert unrelated not in hashed, f"{surface} small metadata access rehashed the unrelated 8 MiB asset"
    assert unrelated not in held, f"{surface} small metadata access retained unrelated asset bytes"


@pytest.mark.parametrize("target", ["unknown", "unsupported", "", None])
def test_explicit_unresolved_sqlproxy_classification_cannot_claim_published_only(target: object) -> None:
    row = copy.deepcopy(PUBLISHED)
    row["connection"].update(server="published.example", powerbi_target=target)
    facts = pda.package_spec_facts({"data_sources": [row]})
    assert not facts.published_only, "published shape cannot repair canonical uncertainty"
    assert not facts.direct_applicable
    if target in ("unknown", "unsupported"):
        assert facts.has_review


def test_handoff_consistency_refusal_preserves_private_policy_and_public_shapes(tmp_path: Path) -> None:
    package = with_data_access(datasource_package(tmp_path / "Source", luid=None), source_rows(LIVE), LIVE_PROJECTION)
    result = pri.verify_phase1_role_identity([package])[0]
    handoff = require_handoff(result.data_access_handoff(package))
    public_role, public_integrity = result.as_dict(), result.verified.integrity.as_dict()
    assert set(public_role["source_identity"]) == {"kind", "sha256", "tableau_luid", "published_key"}
    assert set(public_integrity) == {"status", "files_declared", "files_verified", "findings", "unassessable"}
    assert tuple(field.name for field in fields(handoff)) == ("migration_spec", "facts", "data_access")
    assert handoff.facts._fields == (
        "live_source_keys",
        "has_review",
        "direct_applicable",
        "published_only",
        "refusal_code",
    )
    object.__setattr__(result.brief_policy, "fallback_authorization", "HANDOFF_PRIVATE credential endpoint")
    refused = result.data_access_handoff(package)
    assert_refusal(refused, "package_member_not_verified")
    assert result.as_dict() == public_role and result.verified.integrity.as_dict() == public_integrity
    assert set(refused.as_dict()) == set(public_integrity)
    assert set(refused.as_dict()["findings"][0]) == {"code", "detail"}
    serialized = json.dumps(refused.as_dict())
    assert "HANDOFF_PRIVATE" not in serialized and str(package) not in serialized


@pytest.mark.parametrize("published", [{"luid": DS_LUID + " "}, {"key": " "}])
def test_published_identity_must_be_structurally_valid(published: object) -> None:
    row = {**PUBLISHED, "published_datasource": published}
    facts = pda.package_spec_facts({"data_sources": [row]})
    assert not facts.published_only and not facts.all_flat


@pytest.mark.parametrize(
    "fault,state,code",
    [
        ("spec", "cannot_establish", "spec-unreadable"),
        ("identity", "cannot_establish", "source-key-invalid"),
        ("review", "blocked", "unknown-target"),
        ("inapplicable", "cannot_establish", "projection-invalid"),
        ("published", "cannot_establish", "projection-invalid"),
        ("keys", "cannot_establish", "source-key-set-changed"),
        ("local-over-live", "cannot_establish", "source-key-set-changed"),
        ("live-over-local", "cannot_establish", "source-key-set-changed"),
        ("scope-absent", "blocked", "authorization-mismatch"),
        ("scope-wrong", "blocked", "authorization-mismatch"),
        ("fallback-absent", "blocked", "authorization-mismatch"),
        ("model-only-stop", "blocked", "authorization-mismatch"),
        ("unexpected-provider", "cannot_establish", "provider-foreign"),
    ],
)
def test_reconcile_direct_consistency_refusals_use_existing_codes(fault: str, state: str, code: str) -> None:
    facts = pda.PackageSpecFacts((LIVE_KEY,), False, True, False, None)
    payload, scope, fallback, provider = dict(LIVE_PROJECTION), "model_only", "stop", None
    if fault in ("spec", "identity"):
        facts = facts._replace(refusal_code="spec-unreadable" if fault == "spec" else "source-key-invalid")
    elif fault == "review":
        facts = facts._replace(has_review=True)
    elif fault in ("inapplicable", "published"):
        facts = facts._replace(direct_applicable=False, published_only=fault == "published")
    elif fault == "keys":
        payload["source_keys"] = [OTHER_KEY]
    elif fault == "local-over-live":
        payload = dict(LOCAL_PROJECTION)
    elif fault == "live-over-local":
        facts = facts._replace(live_source_keys=())
    elif fault.startswith("scope-"):
        scope = None if fault == "scope-absent" else "model_and_report"
    elif fault == "fallback-absent":
        fallback = None
    elif fault == "model-only-stop":
        payload = dict(MODEL_ONLY_PROJECTION)
    else:
        provider = (PROVIDER_REF, pda.parse_data_access(json.dumps(LIVE_PROJECTION)))
    assessment = pda.parse_data_access(json.dumps(payload))
    assert assessment.state in pda.DIRECT_ACCEPTED_STATES, "this is a consistency test, not parser rejection"
    result = pda.reconcile_package_data_access(
        assessment, facts, requested_scope=scope, fallback_authorization=fallback, provider=provider
    )
    assert (result.state, result.codes, result.validation, result.max_phase2_claim) == (
        state,
        (code,),
        "not_established",
        "none",
    )
    assert pda.parse_data_access(result.dumps()) == result


@pytest.mark.parametrize(
    "fault,state,code",
    [
        ("missing", "cannot_establish", "provider-missing"),
        ("multiple", "cannot_establish", "provider-foreign"),
        ("token", "cannot_establish", "provider-foreign"),
        ("provider-state", "cannot_establish", "provider-foreign"),
        ("keys", "cannot_establish", "provider-foreign"),
        ("recursive", "cannot_establish", "provider-ambiguous"),
        ("provider-blocked", "cannot_establish", "provider-missing"),
        ("provider-cannot", "cannot_establish", "provider-missing"),
        ("provider-model-only", "blocked", "provider-model-only"),
        ("direct-leg", "cannot_establish", "projection-invalid"),
        ("not-published", "cannot_establish", "projection-invalid"),
        ("review", "blocked", "unknown-target"),
        ("scope", "blocked", "authorization-mismatch"),
        ("fallback-absent", "blocked", "authorization-mismatch"),
    ],
)
def test_reconcile_provider_consistency_refusals_never_strengthen_the_ceiling(
    fault: str, state: str, code: str
) -> None:
    facts = pda.PackageSpecFacts((), False, False, True, None)
    payload, supplied = dict(INHERITED_PROJECTION), dict(LIVE_PROJECTION)
    reference, fallback = PROVIDER_REF, "stop"
    if fault == "token":
        reference = "provider-ref:v1:sha256:" + "f" * 64
    elif fault == "provider-state":
        supplied = dict(LOCAL_PROJECTION)
    elif fault == "keys":
        supplied["source_keys"] = [OTHER_KEY]
    elif fault == "recursive":
        supplied = dict(INHERITED_PROJECTION)
    elif fault.startswith("provider-b") or fault == "provider-cannot":
        supplied.update(
            state="blocked" if fault == "provider-blocked" else "cannot_establish",
            source_keys=[],
            validation="not_established",
            effective_scope=None,
            max_phase2_claim="none",
            codes=["probe-no-credential" if fault == "provider-blocked" else "audit-missing"],
        )
    elif fault == "provider-model-only":
        supplied = dict(MODEL_ONLY_PROJECTION)
    elif fault == "direct-leg":
        facts = facts._replace(live_source_keys=(LIVE_KEY,))
    elif fault == "not-published":
        facts = facts._replace(published_only=False)
    elif fault == "review":
        facts = facts._replace(has_review=True)
    elif fault == "scope":
        payload["effective_scope"] = "model_only"
    elif fault == "fallback-absent":
        fallback = None
    provider = (reference, pda.parse_data_access(json.dumps(supplied)))
    if fault == "missing":
        provider = None
    elif fault == "multiple":
        provider = [provider, provider]
    assessment = pda.parse_data_access(json.dumps(payload))
    assert assessment.state == "provider_inherited", "every wire control must pass the existing parser"
    result = pda.reconcile_package_data_access(
        assessment,
        facts,
        requested_scope=payload["effective_scope"],
        fallback_authorization=fallback,
        provider=provider,
    )
    assert (result.state, result.codes) == (state, (code,))
    assert (result.validation, result.max_phase2_claim) == ("not_established", "none")
    assert assessment.to_json() == payload
    assert pda.parse_data_access(result.dumps()) == result


def test_reconcile_has_no_filesystem_audit_classification_or_brief_parser_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assessment = pda.parse_data_access(json.dumps(INHERITED_PROJECTION))
    supplied = pda.parse_data_access(json.dumps(LIVE_PROJECTION))
    facts = pda.PackageSpecFacts((), False, False, True, None)
    assert tuple(inspect.signature(pda.reconcile_package_data_access).parameters) == (
        "assessment",
        "facts",
        "requested_scope",
        "fallback_authorization",
        "provider",
    )

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the consistency conjunction acquired another authority or performed I/O")

    for name in ("package_spec_facts", "assess_data_access", "read_data_access", "_classify_legs"):
        monkeypatch.setattr(pda, name, forbidden)
    monkeypatch.setattr(pri, "parse_brief_policy", forbidden)
    with monkeypatch.context() as io_patch:
        io_patch.setattr("builtins.open", forbidden)
        for name in ("open", "read_bytes", "read_text", "write_bytes", "write_text", "stat", "lstat"):
            io_patch.setattr(Path, name, forbidden)
        result = pda.reconcile_package_data_access(
            assessment,
            facts,
            requested_scope="report_only_shared_model",
            fallback_authorization="stop",
            provider=(PROVIDER_REF, supplied),
        )
    assert result is assessment


@pytest.mark.parametrize("field,value", [("validation", "unvalidated")])
def test_reconcile_does_not_repair_an_illegal_assessment(field: str, value: str) -> None:
    parsed = pda.parse_data_access(json.dumps(LIVE_PROJECTION))
    invalid = parsed._replace(**{field: value})
    result = pda.reconcile_package_data_access(
        invalid,
        pda.PackageSpecFacts((LIVE_KEY,), False, True, False, None),
        requested_scope="model_only",
        fallback_authorization="stop",
    )
    assert (result.state, result.codes) == ("cannot_establish", ("projection-invalid",))
    assert getattr(invalid, field) == value
