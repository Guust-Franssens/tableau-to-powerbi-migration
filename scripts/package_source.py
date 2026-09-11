"""
purpose: project the exact package-local Tableau source from a root-bound S1/S2 handoff, without I/O.
usage:   resolve_verified_package_source(role_result.source_handoff())
internal: true
internal-reason: only check_reference_readiness consumes this projection; S2 supplies the authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal

PackageKind = Literal["workbook", "datasource"]
SourcePrerequisite = Literal["ready", "blocked", "cannot_establish"]
SourceState = Literal["resolved", "blocked", "cannot_establish"]

CODE_HANDOFF_INVALID = "source_handoff_invalid"


@dataclass(frozen=True)
class PackageSourceInput:
    """S2's own root, role and identity, never independent CLI or diagnostic claims."""

    prerequisite: SourcePrerequisite
    package_root: Path | None = field(repr=False)
    unit: str | None
    kind: PackageKind | None
    asset_path: PurePosixPath | None
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
    if isinstance(value, PackageSourceInput):
        if value.prerequisite in ("blocked", "cannot_establish"):
            return PackageSourceResult(state=value.prerequisite, codes=value.codes)
        scope_valid = (
            value.prerequisite == "ready"
            and not value.codes
            and isinstance(value.package_root, Path)
            and isinstance(value.unit, str)
            and bool(value.unit.strip())
            and value.kind in ("workbook", "datasource")
        )
        role_valid = (
            isinstance(value.asset_path, PurePosixPath)
            and value.asset_path.parts
            and not value.asset_path.is_absolute()
            and ".." not in value.asset_path.parts
            and not any("\\" in part or ":" in part for part in value.asset_path.parts)
            and value.asset_path.suffix.lower()
            in ((".twb", ".twbx") if value.kind == "workbook" else (".tds", ".tdsx"))
        )
        digest_valid = (
            isinstance(value.asset_sha256, str)
            and len(value.asset_sha256) == 64
            and all(char in "0123456789abcdef" for char in value.asset_sha256)
        )
        if scope_valid and role_valid and digest_valid:
            return PackageSourceResult(
                state="resolved",
                path=value.package_root.joinpath(*value.asset_path.parts),
                relative_path=value.asset_path,
                kind=value.kind,
                sha256=value.asset_sha256,
            )
    return PackageSourceResult(state="cannot_establish", codes=(CODE_HANDOFF_INVALID,))
