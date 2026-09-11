"""
purpose: project the exact package-local Tableau source from a root-bound S1/S2 handoff, without I/O.
usage:   resolve_verified_package_source(role_result.source_handoff())
internal: true
internal-reason: only check_reference_readiness consumes this projection; S2 supplies the authority.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, TypeVar

from package_filesystem import is_canonical_key

PackageKind = Literal["workbook", "datasource"]
SourcePrerequisite = Literal["ready", "blocked", "cannot_establish"]
SourceState = Literal["resolved", "blocked", "cannot_establish"]

CODE_HANDOFF_INVALID = "source_handoff_invalid"
CODE_ROOT_BINDING_INVALID = "package_root_binding_invalid"
_Result = TypeVar("_Result")

# Handoff types are exact: accepting subclasses would permit overridden equality or path methods.
# pylint: disable=unidiomatic-typecheck


def exact_root_matches(root: Path | None, identity: str | None) -> bool:
    """Compare a classified target's lexical spelling, never host-dependent Path equality."""
    return type(root) is type(Path()) and type(identity) is str and str(root) == identity


def unique_root_identities(identities: Sequence[str]) -> bool:
    """Refuse duplicate/case-colliding targets; folding detects collisions, never binds results."""
    return all(type(identity) is str and identity for identity in identities) and len(
        {identity.casefold() for identity in identities}
    ) == len(identities)


def bind_root_results(
    identities: Sequence[str],
    results: Sequence[_Result],
    binding_of: Callable[[_Result], tuple[Path, str] | None],
) -> dict[str, _Result] | None:
    """Require a complete exact-root bijection before consuming any authority result."""
    if not unique_root_identities(identities) or len(identities) != len(results):
        return None
    expected = set(identities)
    indexed: dict[str, _Result] = {}
    for result in results:
        binding = binding_of(result)
        if binding is None:
            return None
        root, identity = binding
        if not exact_root_matches(root, identity) or identity not in expected or identity in indexed:
            return None
        indexed[identity] = result
    return indexed


def valid_source_codes(codes: object) -> bool:
    """Codes are an exact, unique tuple of stable identifiers, retained in first-seen order."""
    return (
        type(codes) is tuple
        and all(type(code) is str and re.fullmatch(r"[a-z][a-z0-9_]*", code) for code in codes)
        and len(set(codes)) == len(codes)
    )


@dataclass(frozen=True)
class PackageSourceInput:  # pylint: disable=too-many-instance-attributes
    """S2's own root identity and RAW role spelling, never normalized or diagnostic claims."""

    prerequisite: SourcePrerequisite
    package_root: Path | None = field(repr=False)
    root_identity: str | None = field(repr=False)
    unit: str | None
    kind: PackageKind | None
    asset_path: str | None
    asset_sha256: str | None
    codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageSourceResult:
    """One local source, or the prerequisite refusal; only the relative path is printable."""

    state: SourceState
    path: Path | None = field(default=None, repr=False)
    relative_path: PurePosixPath | None = None
    kind: PackageKind | None = None
    sha256: str | None = None
    codes: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """The public projection deliberately cannot serialize the bound host path."""
        return {
            "state": self.state,
            "path": self.relative_path.as_posix() if self.relative_path is not None else None,
            "kind": self.kind,
            "sha256": self.sha256,
            "codes": list(self.codes),
        }


def resolve_verified_package_source(value: PackageSourceInput | None) -> PackageSourceResult:
    """Copy the authority's one declared source; never discover, verify or repair one.

    Refusals return before inspecting any path. Ready inputs are checked only for an internally
    coherent typed handoff. S1/S2, not this projection, own containment, bytes, roles and identity.
    """
    if type(value) is PackageSourceInput and type(value.prerequisite) is str and valid_source_codes(value.codes):
        if value.prerequisite in ("blocked", "cannot_establish"):
            return PackageSourceResult(state=value.prerequisite, codes=value.codes)
        scope_valid = (
            value.prerequisite == "ready"
            and not value.codes
            and exact_root_matches(value.package_root, value.root_identity)
            and type(value.unit) is str
            and is_canonical_key(value.unit)
            and "/" not in value.unit
            and type(value.kind) is str
            and value.kind in ("workbook", "datasource")
        )
        role_valid = type(value.asset_path) is str and is_canonical_key(value.asset_path)
        digest_valid = (
            type(value.asset_sha256) is str
            and len(value.asset_sha256) == 64
            and all(char in "0123456789abcdef" for char in value.asset_sha256)
        )
        if scope_valid and role_valid and digest_valid:
            path = PurePosixPath(value.asset_path)
            if path.suffix.lower() in ((".twb", ".twbx") if value.kind == "workbook" else (".tds", ".tdsx")):
                return PackageSourceResult(
                    state="resolved",
                    path=value.package_root.joinpath(*path.parts),
                    relative_path=path,
                    kind=value.kind,
                    sha256=value.asset_sha256,
                )
    return PackageSourceResult(state="cannot_establish", codes=(CODE_HANDOFF_INVALID,))
