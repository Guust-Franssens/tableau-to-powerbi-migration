"""Direct controls for #558: source return is a pure projection, not another resolver."""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import sys
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path, PurePath, PurePosixPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bundle_corpus as corpus  # noqa: E402
import check_reference_readiness as readiness  # noqa: E402
import package_filesystem as filesystem  # noqa: E402
import package_role_identity as roles  # noqa: E402
import package_source as source  # noqa: E402
import published_datasource_registry as registry  # noqa: E402
import reference_evidence as evidence  # noqa: E402

ROOT = Path("packages") / "opaque-unit"
SHA = "0123456789abcdef" * 4


class _StringSubclass(str):
    """Equal text is not an exact runtime string field."""


class _TupleSubclass(tuple):
    """A tuple subclass is not the handoff's exact immutable container type."""


INVALID_RAW_PATHS = [
    "",
    ".",
    "..",
    "/assets/opaque.twb",
    "//server/share/opaque.twb",
    "C:/opaque.twb",
    "C:opaque.twb",
    r"\\server\share\opaque.twb",
    r"assets\opaque.twb",
    "assets/opaque.twb:stream",
    "../opaque.twb",
    "assets/../../opaque.twb",
    "./assets/opaque.twb",
    "assets/./opaque.twb",
    "assets/../assets/opaque.twb",
    "assets//opaque.twb",
    "assets/opaque.twb/",
    "assets/CON.twb",
    "assets/con/opaque.twb",
    "assets/PRN.twb",
    "assets/AUX.twb",
    "assets/NUL.twb",
    "assets/CONIN$.twb",
    "assets/CONOUT$.twb",
    "assets/COM1.twb",
    "assets/LPT9.twb",
    "assets/COM\u00b9.twb",
    "assets/COM\u00b2.twb",
    "assets/COM\u00b3.twb",
    "assets/LPT\u00b9.twb",
    "assets/LPT\u00b2.twb",
    "assets/LPT\u00b3.twb",
    "assets/a./opaque.twb",
    "assets/a /opaque.twb",
    "assets/ a/opaque.twb",
    "assets/bad\x00/opaque.twb",
    "assets/bad\x01/opaque.twb",
    "assets/bad\x1f/opaque.twb",
    "assets/bad\x7f/opaque.twb",
    *[f"assets/bad{char}/opaque.twb" for char in '<>:"|?*'],
]


def handoff(kind: source.PackageKind = "workbook", extension: str = ".twb") -> source.PackageSourceInput:
    """A literal authority input: no package writer, discovery or identity inference."""
    return source.PackageSourceInput("ready", ROOT, str(ROOT), "Revenue", kind, f"assets/opaque{extension}", SHA)


def authority(
    root: Path = ROOT, *, kind: source.PackageKind = "workbook", extension: str = ".twb"
) -> roles.Phase1RoleIdentityResult:
    """The existing S2 result, carrying its own S1 observation and one source role."""
    return roles.Phase1RoleIdentityResult(
        verdict="START_READY",
        unit="Revenue" if kind == "workbook" else "Provider",
        kind=kind,
        topology="owned_model" if kind == "workbook" else "published_provider",
        roles=(roles.RoleResult("source_asset", "resolved", "1 file", (f"assets/opaque{extension}",)),),
        source_identity=roles.SourceIdentity(kind, SHA, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", None),
        verified=roles.VerifiedPackage(
            root,
            corpus.TargetClassification("package", "package_declared", "", "none", "opaque-unit"),
            filesystem.PackageFilesystemResult("clean"),
            str(root),
        ),
    )


@pytest.mark.parametrize(
    ("kind", "extension"),
    [("workbook", ".twb"), ("workbook", ".twbx"), ("datasource", ".tds"), ("datasource", ".tdsx")],
)
def test_typed_source_is_exactly_the_declared_role(kind: source.PackageKind, extension: str) -> None:
    value = handoff(kind, extension)

    result = source.resolve_verified_package_source(value)

    assert result.state == "resolved"
    assert result.path == ROOT / "assets" / f"opaque{extension}"
    assert result.relative_path.as_posix() == value.asset_path, "projection must preserve the exact raw role"
    assert result.kind == kind
    assert result.sha256 == SHA
    assert result.codes == ()


def test_inputs_and_results_are_immutable() -> None:
    value = handoff()
    result = source.resolve_verified_package_source(value)

    with pytest.raises(FrozenInstanceError):
        value.unit = "another-unit"
    with pytest.raises(FrozenInstanceError):
        result.path = ROOT / "another.twb"


def test_local_and_server_identity_have_the_same_source_projection() -> None:
    server = authority()
    local = replace(server, source_identity=replace(server.source_identity, tableau_luid=None))

    assert local.source_handoff() == server.source_handoff()
    assert source.resolve_verified_package_source(local.source_handoff()) == source.resolve_verified_package_source(
        server.source_handoff()
    )


def test_diagnostic_handover_provenance_and_provider_are_not_projector_inputs() -> None:
    assert {item.name for item in fields(source.PackageSourceInput)} == {
        "prerequisite",
        "package_root",
        "root_identity",
        "unit",
        "kind",
        "asset_path",
        "asset_sha256",
        "codes",
    }
    assert source.resolve_verified_package_source(handoff()).state == "resolved"


def test_provider_and_consumer_project_their_own_sources() -> None:
    provider = authority(Path("packages") / "Provider", kind="datasource", extension=".tdsx")
    consumer = replace(
        authority(Path("packages") / "Consumer", extension=".twbx"),
        topology="published_consumer",
        dependencies=(
            roles.DependencyResult("resolved", provider_unit="Provider", model_role="fabric/Provider.SemanticModel"),
        ),
    )

    provider_source = source.resolve_verified_package_source(provider.source_handoff())
    consumer_source = source.resolve_verified_package_source(consumer.source_handoff())

    assert (provider_source.path, provider_source.kind) == (
        Path("packages") / "Provider" / "assets" / "opaque.tdsx",
        "datasource",
    )
    assert (consumer_source.path, consumer_source.kind) == (
        Path("packages") / "Consumer" / "assets" / "opaque.twbx",
        "workbook",
    ), "a provider MODEL never substitutes the provider Tableau source for a consumer workbook"


@pytest.mark.parametrize(
    ("prerequisite", "codes"),
    [
        ("cannot_establish", ("package_file_undeclared", "package_file_digest_mismatch")),
        ("blocked", ("role_declaration_absent",)),
        ("blocked", ("server_luid_contradiction",)),
        ("blocked", ("provider_ambiguous",)),
    ],
)
def test_refusal_codes_pass_through_without_inspecting_paths(prerequisite: str, codes: tuple[str, ...]) -> None:
    value = source.PackageSourceInput(prerequisite, object(), object(), None, None, object(), None, codes)

    result = source.resolve_verified_package_source(value)

    assert result.state == prerequisite
    assert result.codes is codes
    assert (result.path, result.relative_path, result.kind, result.sha256) == (None, None, None, None)


def test_s2_handoff_keeps_fresh_s1_codes_instead_of_a_generic_s2_block() -> None:
    result = authority()
    s1 = filesystem.PackageFilesystemResult(
        "findings", findings=(filesystem.Finding("package_file_undeclared", "fixed diagnostic", "foreign.txt"),)
    )
    result = replace(
        result,
        verdict="BLOCKED",
        blockers=("package_integrity_not_clean",),
        verified=replace(result.verified, integrity=s1),
    )

    projected = source.resolve_verified_package_source(result.source_handoff())

    assert projected.state == "cannot_establish"
    assert projected.codes == ("package_file_undeclared",)
    assert projected.path is None


@pytest.mark.parametrize(
    "changes",
    [
        {"prerequisite": "unknown"},
        {"prerequisite": True},
        {"prerequisite": _StringSubclass("ready")},
        {"package_root": None},
        {"package_root": "packages/opaque-unit"},
        {"root_identity": None},
        {"root_identity": str(ROOT).upper()},
        {"root_identity": _StringSubclass(str(ROOT))},
        {"unit": None},
        {"unit": ""},
        {"unit": " "},
        {"unit": False},
        {"unit": _StringSubclass("Revenue")},
        {"kind": None},
        {"kind": "unknown"},
        {"kind": False},
        {"kind": _StringSubclass("workbook")},
        {"asset_path": None},
        {"asset_path": PurePosixPath("assets/opaque.twb")},
        {"asset_path": PurePosixPath("assets/./opaque.twb")},
        {"asset_path": PurePosixPath("assets//opaque.twb")},
        {"asset_path": False},
        {"asset_path": _StringSubclass("assets/opaque.twb")},
        {"asset_path": "assets/opaque.tdsx"},
        {"asset_sha256": None},
        {"asset_sha256": False},
        {"asset_sha256": _StringSubclass(SHA)},
        {"asset_sha256": "f" * 63},
        {"asset_sha256": "g" * 64},
        {"asset_sha256": "F" * 64},
        {"codes": ("provider_missing",)},
        {"codes": False},
        {"codes": []},
        {"codes": ""},
        {"codes": None},
        {"codes": _TupleSubclass()},
    ],
)
def test_malformed_ready_handoff_cannot_establish_and_never_returns_a_source(
    changes: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = project_without_io(monkeypatch, replace(handoff(), **changes))

    assert result.state == "cannot_establish"
    assert result.codes == ("source_handoff_invalid",)
    assert (result.path, result.relative_path, result.kind, result.sha256) == (None, None, None, None)


@pytest.mark.parametrize("raw", INVALID_RAW_PATHS)
def test_raw_role_is_refused_before_path_normalization(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    value = replace(handoff(), asset_path=raw)

    def forbidden(*_args: object) -> object:
        raise _ForbiddenSourceOperation("invalid raw spelling reached the normalizing path constructor")

    monkeypatch.setattr(source, "PurePosixPath", forbidden)
    result = project_without_io(monkeypatch, value)

    assert result.state == "cannot_establish"
    assert result.codes == ("source_handoff_invalid",)
    assert result.path is None


@pytest.mark.parametrize("raw", ["./assets/opaque.twb", "assets/./opaque.twb", "assets//opaque.twb"])
def test_s2_preserves_raw_aliases_until_the_projector_refuses_them(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    result = replace(authority(), roles=(roles.RoleResult("source_asset", "resolved", "1 file", (raw,)),))
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        value = result.source_handoff()

    assert type(value.asset_path) is str
    assert value.asset_path is raw, "S2 must not erase a raw alias with PurePosixPath"
    assert project_without_io(monkeypatch, value).codes == ("source_handoff_invalid",)


@pytest.mark.parametrize("raw", ["source.twb", "assets/nested/Report.v2.twb", "assets/Revenue 2026.TWBX"])
def test_canonical_raw_paths_are_projected_without_io(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    result = project_without_io(monkeypatch, replace(handoff(), asset_path=raw))

    assert result.state == "resolved"
    assert result.relative_path.as_posix() == raw
    assert str(result.path) == str(ROOT.joinpath(*raw.split("/")))
    assert result.codes == ()


@pytest.mark.parametrize("prerequisite", ["ready", "blocked", "cannot_establish"])
@pytest.mark.parametrize(
    "codes",
    [
        False,
        None,
        "",
        "provider_missing",
        [],
        ["provider_missing"],
        _TupleSubclass(),
        (None,),
        (False,),
        ([],),
        ("",),
        ("UPPER",),
        ("not-a-code",),
        ("code\n",),
        (_StringSubclass("provider_missing"),),
        ("provider_missing", "provider_missing"),
    ],
)
def test_wrong_code_types_or_duplicate_codes_never_pass_through(
    prerequisite: str, codes: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = project_without_io(monkeypatch, replace(handoff(), prerequisite=prerequisite, codes=codes))

    assert result.state == "cannot_establish"
    assert result.codes == ("source_handoff_invalid",)
    assert result.path is None


@pytest.mark.parametrize(
    "changes",
    [
        {"verified": None},
        {"verified": object()},
        {"verdict": _StringSubclass("START_READY")},
        {"blockers": False},
        {"blockers": []},
        {"blockers": ""},
        {"blockers": None},
        {"roles": ()},
        {"roles": (roles.RoleResult("source_asset", "missing", "1 file"),)},
        {"roles": (roles.RoleResult("source_asset", "resolved", "1 file", ("assets/a.twb", "assets/b.twb")),)},
        {"roles": (roles.RoleResult("source_asset", "resolved", "1 file", ["assets/a.twb"]),)},
        {"source_identity": None},
        {"source_identity": roles.SourceIdentity("datasource", SHA, None, None)},
        {"source_identity": roles.SourceIdentity(_StringSubclass("workbook"), SHA, None, None)},
    ],
)
def test_inconsistent_s2_result_cannot_mint_a_usable_handoff(changes: dict[str, object]) -> None:
    result = source.resolve_verified_package_source(replace(authority(), **changes).source_handoff())

    assert result.state == "cannot_establish"
    assert result.codes == ("source_handoff_invalid",)
    assert result.path is None


def test_missing_handoff_is_not_a_legacy_search_request() -> None:
    result = source.resolve_verified_package_source(None)

    assert result.state == "cannot_establish"
    assert result.codes == ("source_handoff_invalid",)


class _ForbiddenSourceOperation(AssertionError):
    """Caught before undoing global patches, so a mutation yields an assertion, not a pytest error."""


def forbid_source_operations(patch: pytest.MonkeyPatch) -> None:
    """Arm filesystem, ancestor, parser, hash, legacy and registry entry points at once."""

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise _ForbiddenSourceOperation("pure source projection attempted a forbidden operation")

    for name in (
        "resolve",
        "absolute",
        "expanduser",
        "exists",
        "is_file",
        "is_dir",
        "stat",
        "lstat",
        "glob",
        "rglob",
        "iterdir",
        "read_text",
        "read_bytes",
        "open",
        "samefile",
    ):
        patch.setattr(Path, name, forbidden)
    for name in ("parent", "parents"):
        patch.setattr(PurePath, name, property(forbidden))
    for module, names in (
        (builtins, ("open",)),
        (io, ("open",)),
        (os, ("open", "stat", "lstat", "scandir", "listdir", "walk")),
        (json, ("load", "loads")),
        (hashlib, ("sha256", "md5", "new", "file_digest")),
        (filesystem, ("verify_package", "walk_package", "parse_manifest_text", "declared_files")),
        (corpus, ("classify_target", "shipping_reports", "evidence_dirs")),
        (readiness, ("resolve_source", "_identify", "_handover", "json_object", "sha256_of", "provenance_origin")),
        (evidence, ("json_object", "sha256_of", "provenance_origin")),
        (registry, ("find_shared_models", "scan_contracts", "build_index", "_reuse_candidate", "_near_misses")),
    ):
        for name in names:
            patch.setattr(module, name, forbidden)


def project_without_io(
    monkeypatch: pytest.MonkeyPatch, value: source.PackageSourceInput | None
) -> source.PackageSourceResult:
    """Undo global patches before any failed purity assertion reaches pytest's own readers."""
    result, violation = None, None
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        try:
            result = source.resolve_verified_package_source(value)
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)
    assert violation is None, violation
    assert result is not None
    return result


@pytest.mark.parametrize(
    ("value", "state", "codes"),
    [
        (handoff("workbook", ".twb"), "resolved", ()),
        (handoff("workbook", ".twbx"), "resolved", ()),
        (handoff("datasource", ".tds"), "resolved", ()),
        (handoff("datasource", ".tdsx"), "resolved", ()),
        (
            replace(handoff(), prerequisite="blocked", codes=("role_declaration_absent",)),
            "blocked",
            ("role_declaration_absent",),
        ),
        (
            replace(handoff(), prerequisite="cannot_establish", codes=("package_file_undeclared",)),
            "cannot_establish",
            ("package_file_undeclared",),
        ),
        (replace(handoff(), asset_path=None), "cannot_establish", ("source_handoff_invalid",)),
        (None, "cannot_establish", ("source_handoff_invalid",)),
    ],
    ids=["twb", "twbx", "tds", "tdsx", "blocked", "s1-refused", "malformed", "absent"],
)
def test_projector_completes_with_every_forbidden_operation_armed(
    monkeypatch: pytest.MonkeyPatch, value: source.PackageSourceInput | None, state: str, codes: tuple[str, ...]
) -> None:
    result = project_without_io(monkeypatch, value)
    assert result.state == state
    assert result.codes == codes


def test_tempting_undeclared_file_does_not_change_a_blocked_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = tmp_path / "assets" / "Revenue.twb"
    candidate.parent.mkdir()
    candidate.write_text("<workbook/>", encoding="utf-8")
    value = replace(
        handoff(), package_root=tmp_path, prerequisite="blocked", asset_path=None, codes=("role_declaration_absent",)
    )
    violation, result = None, None
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        try:
            result = source.resolve_verified_package_source(value)
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert result is not None and result.state == "blocked"
    assert result.path is None
    assert result.codes == ("role_declaration_absent",)


def test_printable_source_contains_only_the_relative_role(tmp_path: Path) -> None:
    result = source.resolve_verified_package_source(
        replace(handoff(), package_root=tmp_path, root_identity=str(tmp_path))
    )

    assert result.as_dict() == {
        "state": "resolved",
        "path": "assets/opaque.twb",
        "kind": "workbook",
        "sha256": SHA,
        "codes": [],
    }
    assert str(tmp_path) not in json.dumps(result.as_dict())
    assert str(tmp_path) not in repr(result)
