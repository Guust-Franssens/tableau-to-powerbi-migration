"""Direct controls for #558: source return is a pure projection, not another resolver."""

from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import sys
from dataclasses import FrozenInstanceError, replace
from pathlib import Path, PurePath

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


def test_inputs_and_results_are_immutable() -> None:
    value = handoff()
    result = source.resolve_verified_package_source(value)

    with pytest.raises(FrozenInstanceError):
        value.unit = "another-unit"
    with pytest.raises(FrozenInstanceError):
        result.path = ROOT / "another.twb"


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


@pytest.mark.parametrize("changes", [{"asset_path": "assets/opaque.tdsx"}])
def test_malformed_ready_handoff_cannot_establish_and_never_returns_a_source(
    changes: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = project_without_io(monkeypatch, replace(handoff(), **changes))

    assert result.state == "cannot_establish"
    assert result.codes == ("source_handoff_invalid",)
    assert (result.path, result.relative_path, result.kind, result.sha256) == (None, None, None, None)


@pytest.mark.parametrize(
    "raw", ["/assets/opaque.twb", "assets/a./opaque.twb", "assets/bad\x1f/opaque.twb", "assets/bad\x7f/opaque.twb"]
)
def test_raw_role_is_refused_before_path_normalization(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    value = replace(handoff(), asset_path=raw)

    def forbidden(*_args: object) -> object:
        raise _ForbiddenSourceOperation("invalid raw spelling reached the normalizing path constructor")

    monkeypatch.setattr(source, "PurePosixPath", forbidden)
    result = project_without_io(monkeypatch, value)

    assert result.state == "cannot_establish"
    assert result.codes == ("source_handoff_invalid",)
    assert result.path is None


@pytest.mark.parametrize("raw", ["source.twb"])
def test_canonical_raw_paths_are_projected_without_io(raw: str, monkeypatch: pytest.MonkeyPatch) -> None:
    result = project_without_io(monkeypatch, replace(handoff(), asset_path=raw))

    assert result.state == "resolved"
    assert result.relative_path.as_posix() == raw
    assert str(result.path) == str(ROOT.joinpath(*raw.split("/")))
    assert result.codes == ()


@pytest.mark.parametrize(
    "changes",
    [
        {"verified": object()},
        {"verdict": _StringSubclass("START_READY")},
        {"blockers": None},
        {"roles": ()},
        {"roles": (roles.RoleResult("source_asset", "missing", "1 file"),)},
        {"roles": (roles.RoleResult("source_asset", "resolved", "1 file", ("assets/a.twb", "assets/b.twb")),)},
        {"roles": (roles.RoleResult("source_asset", "resolved", "1 file", ["assets/a.twb"]),)},
        {"source_identity": roles.SourceIdentity(_StringSubclass("workbook"), SHA, None, None)},
    ],
)
def test_inconsistent_s2_result_cannot_mint_a_usable_handoff(changes: dict[str, object]) -> None:
    result = source.resolve_verified_package_source(replace(authority(), **changes).source_handoff())

    assert result.state == "cannot_establish"
    assert result.codes == ("source_handoff_invalid",)
    assert result.path is None


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
        (
            replace(handoff(), prerequisite="blocked", codes=("role_declaration_absent",)),
            "blocked",
            ("role_declaration_absent",),
        ),
    ],
    ids=["twb", "blocked"],
)
def test_projector_completes_with_every_forbidden_operation_armed(
    monkeypatch: pytest.MonkeyPatch, value: source.PackageSourceInput | None, state: str, codes: tuple[str, ...]
) -> None:
    result = project_without_io(monkeypatch, value)
    assert result.state == state
    assert result.codes == codes


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
