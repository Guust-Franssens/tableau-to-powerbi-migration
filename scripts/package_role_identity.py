"""
purpose: prove required package roles and cross-artifact identity over an exact-root cohort.
usage:   import package_role_identity as pri; pri.verify_phase1_role_identity([Path("packages/Unit")])

S2 (#562) re-runs no-follow S1 even when earlier observations are supplied. Both sets must biject
with the original lexical roots. Roles are declarations, never discovery; only walked paths open.
Provider closure uses datasource LUID, then exact published key only when BOTH sides lack a LUID.
Display names and paths never supply identity; a consumer alone blocks.
source_handoff() retains the bound root and RAW asset role for #558's pure projector. Brief policy
and the selected provider's input ordinal travel only in memory. No source Path, search, credentials,
evidence grade, final START_READY fold or package writes here.
Full contract and limitations: docs/reference-readiness.md, S2.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tomllib
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

import package_filesystem as pfs  # noqa: E402  # pylint: disable=wrong-import-position
import tableau_env  # noqa: E402  # pylint: disable=wrong-import-position
from bundle_corpus import (  # noqa: E402  # pylint: disable=wrong-import-position
    TargetClassification,
    classify_target,
)
from credential_gate import PackageSpecFacts, package_spec_facts  # noqa: E402  # pylint: disable=wrong-import-position
from host_paths import discloses_host_location  # noqa: E402  # pylint: disable=wrong-import-position
from package_source import (  # noqa: E402  # pylint: disable=wrong-import-position
    CODE_HANDOFF_INVALID,
    CODE_ROOT_BINDING_INVALID,
    PackageKind,
    PackageSourceInput,
    bind_root_results,
    exact_root_matches,
    unique_root_identities,
    valid_source_codes,
)
from reference_evidence import (  # noqa: E402  # pylint: disable=wrong-import-position
    REVISION_UNCONFIRMED,
    revision_status,
)

_ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

# pylint: disable=unidiomatic-typecheck  # The handoff must reject subclass coercion.

RoleState = Literal["resolved", "not_applicable", "missing", "ambiguous", "mismatch"]
Topology = Literal["owned_model", "standalone_datasource", "published_provider", "published_consumer"]

#: One admissible role, all applicable identity claims agreeing. The only passing state.
STATE_RESOLVED = "resolved"
#: EARNED from kind/topology - never inferred from absence.
STATE_NOT_APPLICABLE = "not_applicable"
#: Zero admissible candidates for a required role.
STATE_MISSING = "missing"
#: More than one admissible candidate, provider or identity remains.
STATE_AMBIGUOUS = "ambiguous"
#: The artifacts are present and their stable claims disagree.
STATE_MISMATCH = "mismatch"

#: Every role state that blocks. ``not_applicable`` has to be earned, which is why it is not here.
BLOCKING_STATES = frozenset({STATE_MISSING, STATE_AMBIGUOUS, STATE_MISMATCH})

KIND_WORKBOOK = "workbook"
KIND_DATASOURCE = "datasource"

TOPOLOGY_OWNED_MODEL = "owned_model"
TOPOLOGY_STANDALONE_DATASOURCE = "standalone_datasource"
TOPOLOGY_PUBLISHED_PROVIDER = "published_provider"
TOPOLOGY_PUBLISHED_CONSUMER = "published_consumer"

VERDICT_START_READY = "START_READY"
VERDICT_BLOCKED = "BLOCKED"

ROLE_MIGRATION_BRIEF = "migration_brief"
ROLE_SOURCE_ASSET = "source_asset"
ROLE_SOURCE_PROVENANCE = "source_provenance"
ROLE_SOURCE_IDENTITY = "source_identity"
ROLE_SERVER_IDENTITY = "server_identity"
ROLE_MIGRATION_SPEC = "migration_spec"
ROLE_MIGRATION_SPEC_SCHEMA = "migration_spec_schema"
ROLE_ENGINE_CLASSIFICATION = "engine_classification"
ROLE_HANDOVER = "handover"
ROLE_ENGINE_RECEIPT = "engine_receipt"
ROLE_FABRIC_REPORT = "fabric_report"
ROLE_FABRIC_MODEL = "fabric_model"
ROLE_PBIP_ENTRYPOINT = "pbip_entrypoint"
ROLE_TABLEAU_REFERENCE = "tableau_reference"
ROLE_TABLEAU_ORACLE = "tableau_oracle"
ROLE_VISUAL_EVIDENCE = "visual_evidence"
ROLE_PUBLISHED_DEPENDENCY = "published_dependency"

#: The source-role extensions each kind may carry. A `.tds` in a workbook package is not a source
#: role that happens to be unusual - it is the wrong artifact, and the unit is not what it says.
SOURCE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    KIND_WORKBOOK: (".twb", ".twbx"),
    KIND_DATASOURCE: (".tds", ".tdsx"),
}

#: The one package-relative name a packaged brief may have. Named rather than discovered so that
#: "the brief is present" cannot be satisfied by any other Markdown file a package happens to carry.
BRIEF_NAME = "migration-brief.md"
SPEC_NAME = "migration-spec.json"
DATA_ACCESS_NAME = "data-access.json"

#: The brief's explicit scope must agree with topology; it is never inferred as authorization.
TOPOLOGY_SCOPE = {
    TOPOLOGY_OWNED_MODEL: "model_and_report",
    TOPOLOGY_STANDALONE_DATASOURCE: "model_only",
    TOPOLOGY_PUBLISHED_PROVIDER: "model_only",
    TOPOLOGY_PUBLISHED_CONSUMER: "report_only_shared_model",
}

#: Recorded when a source is genuinely local: no Tableau Server LUID exists to agree with, and the
#: SHA/filename/spec axes all do. It does NOT convert a role state - it says why one is N/A.
LIMITATION_LOCAL_SOURCE = "local_source_no_server_luid"
#: Plain/legacy/missing briefs establish no typed policy. Markdown never authorizes a fallback.
LIMITATION_BRIEF_POLICY_UNPARSED = "brief_policy_not_parsed"

# Stable diagnostic codes. ⚠️ These are printed into shared verdicts: no host path, no customer
# text, no exception message. A package-RELATIVE path may travel in `RoleResult.paths`, because the
# package already publishes those in its own manifest.
CODE_NOT_A_PACKAGE = "package_boundary_not_declared"
CODE_UNSAFE_TARGET = "package_boundary_unsafe"
CODE_INTEGRITY_NOT_CLEAN = "package_integrity_not_clean"
CODE_INTEGRITY_CHANGED = "package_changed_since_integrity_check"
CODE_MANIFEST_UNREADABLE = "package_manifest_unreadable"
CODE_IDENTITY_JSON = "identity_json_invalid"
CODE_IDENTITY_TYPE = "identity_type_invalid"
CODE_UNIT_MISSING = "package_unit_missing"
CODE_KIND_UNCLASSIFIED = "package_kind_unclassified"
# ⚠️ There is deliberately NO "duplicate unit in the cohort" code. `unit` is a package-LOCAL scope
# key derived from a Tableau display name, and two genuinely distinct workbooks may share one - the
# case `_runs/<NNN>-<slug>` numbering exists for elsewhere in this repo. Refusing a cohort on that
# basis would be a name join wearing a collision check, and it would refuse exactly the shape the
# "duplicate display names, distinct LUID/SHA" control requires to resolve. Real collisions are
# caught where identities actually collide: two providers answering one LUID or key.
CODE_ROLE_MISSING = "role_missing"
CODE_ROLE_AMBIGUOUS = "role_ambiguous"
CODE_ROLE_UNDECLARED = "role_declaration_absent"
CODE_ROLE_NOT_VERIFIED = "role_declaration_not_a_verified_file"
CODE_ROLE_WRONG_CANDIDATE = "role_declaration_not_an_admissible_candidate"
CODE_SCOPE_MISMATCH = "package_scope_mismatch"
CODE_ENGINE_MEMBERSHIP = "engine_classification_membership"
CODE_SOURCE_SHA_MISSING = "source_sha_missing"
CODE_PROVENANCE_ROWS = "provenance_row_cardinality"
CODE_PROVENANCE_SHA = "provenance_sha_disagrees_with_source"
CODE_PROVENANCE_FILE = "provenance_file_disagrees_with_source"
CODE_SPEC_FILE = "spec_file_disagrees_with_source"
CODE_LUID_NAMESPACE = "luid_namespace_mismatch"
CODE_LUID_CONTRADICTION = "server_luid_contradiction"
CODE_LUID_UNREPRESENTED = "server_luid_unrepresented"
CODE_RECEIPT_SCOPE = "receipt_scope_mismatch"
CODE_RECEIPT_FOREIGN_OUTPUT = "receipt_output_outside_package"
CODE_RECEIPT_UNCOVERED = "receipt_does_not_cover_role"
CODE_EVIDENCE_FOREIGN = "evidence_foreign_identity"
CODE_EVIDENCE_UNACCOUNTED = "evidence_file_unaccounted"
CODE_EVIDENCE_MANIFEST = "evidence_manifest_unreadable"
CODE_EVIDENCE_PATH = "evidence_path_not_verified"
CODE_INAPPLICABLE_PRESENT = "inapplicable_role_present"
CODE_BRIEF_FRONTMATTER = "brief_frontmatter_unparseable"
CODE_BRIEF_UNIT = "brief_unit_mismatch"
CODE_BRIEF_SCOPE = "brief_scope_mismatch"
CODE_BRIEF_UNSAFE = "brief_contains_unsafe_text"
CODE_BRIEF_POLICY = "brief_policy_invalid"
CODE_DEPENDENCY_IDENTITY = "published_dependency_identity_missing"
CODE_DEPENDENCY_INVALID = "published_dependency_invalid"
CODE_PROVIDER_MISSING = "provider_missing"
CODE_PROVIDER_AMBIGUOUS = "provider_ambiguous"
CODE_PROVIDER_LUID_CONTRADICTION = "provider_luid_contradiction"
CODE_PROVIDER_KEY_CONTRADICTION = "provider_key_contradiction"
CODE_PROVIDER_BINDING = "provider_binding_mismatch"
CODE_PROVIDER_MODEL = "provider_model_unresolved"
CODE_PROVIDER_BLOCKED = "provider_not_s2_clean"


@dataclass(frozen=True)
class BriefPolicy:
    """The exact brief's explicit policy, not an inferred scope or an earned data-access verdict."""

    requested_scope: str
    fallback_authorization: str


@dataclass(frozen=True)
class RoleResult:
    """One semantic role's state, its expected cardinality, and the package-relative paths it holds."""

    role: str
    state: RoleState
    cardinality: str
    paths: tuple[str, ...] = ()
    code: str | None = None

    @property
    def blocks(self) -> bool:
        """Whether this role alone refuses START_READY."""
        return self.state in BLOCKING_STATES

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape a consumer embeds - stable codes and package-relative paths only."""
        return {
            "role": self.role,
            "state": self.state,
            "cardinality": self.cardinality,
            "paths": list(self.paths),
            "code": self.code,
        }


@dataclass(frozen=True)
class SourceIdentity:
    """S1's source digest, this kind's Tableau LUID (None for local), and exact published key."""

    kind: PackageKind
    sha256: str | None
    tableau_luid: str | None
    published_key: str | None
    revision: str = REVISION_UNCONFIRMED

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape a consumer embeds."""
        return {
            "kind": self.kind,
            "sha256": self.sha256,
            "tableau_luid": self.tableau_luid,
            "published_key": self.published_key,
        }


@dataclass(frozen=True)
class DependencyResult:
    """One published-datasource edge, resolved inside the cohort or refused with a reason."""

    state: RoleState
    datasource_luid: str | None = None
    published_key: str | None = None
    provider_unit: str | None = None
    model_role: str | None = None
    code: str | None = None
    provider_ordinal: int | None = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        """The JSON shape a consumer embeds."""
        return {
            "state": self.state,
            "datasource_luid": self.datasource_luid,
            "published_key": self.published_key,
            "provider_unit": self.provider_unit,
            "model_role": self.model_role,
            "code": self.code,
        }


@dataclass(frozen=True)
class DeclaredDependency:
    """One declared row, including rows whose identity cannot be established."""

    luid: str | None = None
    key: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class PackageEvidence:
    """An assessed evidence record and its walk-produced render path, not a grade or a source."""

    origin: Literal["reference", "oracle"]
    manifest: dict[str, Any]
    entry: dict[str, Any]
    state: dict[str, Any]
    render_path: Path | None


@dataclass(frozen=True)
class _DataAccessSnapshot:
    """Keep the parsed spec facts and the declared projection attached to their exact S1 result."""

    integrity: pfs.PackageFilesystemResult = field(repr=False)
    spec: pfs.HeldVerifiedMember = field(repr=False)
    facts: PackageSpecFacts
    declared: bool


@dataclass(frozen=True)
class PackageDataAccessHandoff:
    """Only two held roles and current source facts from the same S1/S2 snapshot; no semantic fold."""

    migration_spec: pfs.HeldVerifiedMember = field(repr=False)
    facts: PackageSpecFacts
    data_access: pfs.HeldVerifiedMember = field(repr=False)


@dataclass(frozen=True)
class Phase1RoleIdentityResult:  # pylint: disable=too-many-instance-attributes
    """One package's role/identity verdict. Emitted by the final gate; never written into a package."""

    verdict: Literal["START_READY", "BLOCKED"]
    unit: str | None
    kind: PackageKind | None
    topology: Topology | None
    roles: tuple[RoleResult, ...] = ()
    source_identity: SourceIdentity | None = None
    dependencies: tuple[DependencyResult, ...] = ()
    blockers: tuple[str, ...] = ()
    authorized_limitations: tuple[str, ...] = ()
    evidence: tuple[PackageEvidence, ...] = field(default=(), repr=False, compare=False)
    verified: VerifiedPackage | None = field(default=None, repr=False, compare=False)
    brief_policy: BriefPolicy | None = field(default=None, repr=False, compare=False)
    _data_access_snapshot: _DataAccessSnapshot | None = field(default=None, repr=False, compare=False)
    _authority: Callable[[object], bool] | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def is_start_ready(self) -> bool:
        """Whether every required role resolved or earned its `not_applicable`."""
        return self.verdict == VERDICT_START_READY

    def codes(self) -> tuple[str, ...]:
        """Every distinct blocking code, in first-seen order."""
        seen: list[str] = []
        for code in self.blockers:
            if code not in seen:
                seen.append(code)
        return tuple(seen)

    def source_handoff(self) -> PackageSourceInput:
        """Preserve this result's own S1 root and raw role; malformed ready results cannot resolve."""
        if (
            type(self.verdict) is not str
            or self.verdict not in (VERDICT_START_READY, VERDICT_BLOCKED)
            or not valid_source_codes(self.blockers)
        ):
            return PackageSourceInput("cannot_establish", None, None, None, None, None, None, (CODE_HANDOFF_INVALID,))
        verified = self.verified if type(self.verified) is VerifiedPackage else None
        root = verified.root if verified is not None else None
        root_identity = verified.root_identity if verified is not None else None
        if verified is not None and not verified.integrity.is_clean:
            return PackageSourceInput(
                "cannot_establish", root, root_identity, self.unit, self.kind, None, None, verified.integrity.codes()
            )
        if not self.is_start_ready:
            return PackageSourceInput("blocked", root, root_identity, self.unit, self.kind, None, None, self.blockers)
        assets = (
            [role for role in self.roles if type(role.role) is str and role.role == ROLE_SOURCE_ASSET]
            if type(self.roles) is tuple and all(type(role) is RoleResult for role in self.roles)
            else []
        )
        path = (
            assets[0].paths[0]
            if len(assets) == 1
            and type(assets[0].state) is str
            and assets[0].state == STATE_RESOLVED
            and type(assets[0].paths) is tuple
            and len(assets[0].paths) == 1
            else None
        )
        identity = self.source_identity
        digest = (
            identity.sha256
            if type(identity) is SourceIdentity and type(identity.kind) is str and identity.kind == self.kind
            else None
        )
        return PackageSourceInput("ready", root, root_identity, self.unit, self.kind, path, digest, self.blockers)

    def data_access_handoff(  # pylint: disable=too-many-return-statements
        self, root: Path
    ) -> PackageDataAccessHandoff | pfs.PackageFilesystemResult:
        """Obtain the exact declared projection, with the spec bytes/facts already used by S2.

        The caller cannot substitute a root or obtain an undeclared file by its familiar name.
        S1 rechecks only the two small roles and manifest, not unrelated asset content. The held
        spec is not reparsed/reclassified. Copies cannot borrow the original role result's authority.
        Renewal requires fresh S1 and S2, not a fresh S1 grafted into the issued S2 result.
        """
        verified = self.verified
        if type(verified) is not VerifiedPackage or not verified.is_bound_to(str(root)):
            return pfs.member_refusal(pfs.CODE_ROOT_BINDING)
        snapshot = self._data_access_snapshot
        if not self.is_start_ready or type(snapshot) is not _DataAccessSnapshot or not snapshot.declared:
            return pfs.member_refusal()
        if snapshot.integrity is not verified.integrity or snapshot.spec.root_identity != verified.root_identity:
            return pfs.member_refusal(pfs.CODE_ROOT_BINDING)
        if not self._has_handoff_authority():
            return pfs.member_refusal()
        if not any(
            member.relative_path == snapshot.spec.relative_path == SPEC_NAME and member.sha256 == snapshot.spec.sha256
            for member in verified.integrity.verified_files
        ):
            return pfs.member_refusal()
        if (
            not isinstance(snapshot.spec.content, bytes)
            or hashlib.sha256(snapshot.spec.content).hexdigest() != snapshot.spec.sha256
        ):
            return pfs.member_refusal(pfs.CODE_DIGEST_MISMATCH)
        current_spec = verified.read_verified_member(root, SPEC_NAME)
        if isinstance(current_spec, pfs.PackageFilesystemResult):
            return current_spec
        held = verified.read_verified_member(root, DATA_ACCESS_NAME)
        if isinstance(held, pfs.PackageFilesystemResult):
            return held
        return PackageDataAccessHandoff(snapshot.spec, snapshot.facts, held)

    def _has_handoff_authority(self) -> bool:
        try:
            return self._authority is not None and self._authority(self)
        except (AttributeError, TypeError):
            return False

    def as_dict(self) -> dict[str, Any]:
        """The machine-readable shape the entry gate embeds in its verdict."""
        return {
            "verdict": self.verdict,
            "unit": self.unit,
            "kind": self.kind,
            "topology": self.topology,
            "roles": [role.as_dict() for role in self.roles],
            "source_identity": self.source_identity.as_dict() if self.source_identity else None,
            "dependencies": [dep.as_dict() for dep in self.dependencies],
            "blockers": list(self.blockers),
            "authorized_limitations": list(self.authorized_limitations),
        }


@dataclass(frozen=True)
class VerifiedPackage:
    """An earlier S1 observation, never authority to skip verification at the S2 read seam."""

    root: Path
    classification: TargetClassification
    integrity: pfs.PackageFilesystemResult
    root_identity: str = field(repr=False)

    def is_bound_to(self, identity: str) -> bool:
        """The same exact classified root must own both the observation and its handoff."""
        return (
            type(identity) is str
            and exact_root_matches(self.root, self.root_identity)
            and self.root_identity == identity
        )

    def read_verified_member(
        self, root: Path, relative_path: str
    ) -> pfs.HeldVerifiedMember | pfs.PackageFilesystemResult:
        """Read only through this observation's exact root-bound S1 namespace."""
        if not self.is_bound_to(str(root)):
            return pfs.member_refusal(pfs.CODE_ROOT_BINDING)
        return pfs.read_verified_member(root, self.integrity, relative_path)


def _handoff_authority_state(result: Phase1RoleIdentityResult) -> tuple:
    """Capture scalar role/fact values without re-running any role or connection classifier."""
    snapshot = result._data_access_snapshot  # pylint: disable=protected-access
    identity, policy = result.source_identity, result.brief_policy
    return (
        result.verdict,
        result.unit,
        result.kind,
        result.topology,
        result.blockers,
        result.authorized_limitations,
        tuple((row.role, row.state, row.cardinality, row.paths, row.code) for row in result.roles),
        (
            identity.kind,
            identity.sha256,
            identity.tableau_luid,
            identity.published_key,
            identity.revision,
        )
        if identity is not None
        else None,
        tuple(
            (
                row.state,
                row.datasource_luid,
                row.published_key,
                row.provider_unit,
                row.model_role,
                row.code,
                row.provider_ordinal,
            )
            for row in result.dependencies
        ),
        (policy.requested_scope, policy.fallback_authorization) if policy is not None else None,
        snapshot.declared,
        (snapshot.spec.relative_path, snapshot.spec.sha256, snapshot.spec.root_identity),
    )


def verify_s1(root: Path) -> VerifiedPackage:
    """Classify ``root`` and verify its manifest against its bytes - the S1 prerequisite, bound."""
    classification = classify_target(root)
    root_identity = str(root)
    integrity = pfs.verify_package(root, classification)
    return VerifiedPackage(root=root, classification=classification, integrity=integrity, root_identity=root_identity)


def verified_root_binding(value: VerifiedPackage | None) -> tuple[Path, str] | None:
    """Expose only this typed observation's own lexical binding, without inspecting the root."""
    return (value.root, value.root_identity) if type(value) is VerifiedPackage else None


@dataclass
class _Facts:  # pylint: disable=too-many-instance-attributes,attribute-defined-outside-init
    """Everything one package says about itself, read once from S1-verified bytes."""

    root: Path
    unit: str
    kind: str
    manifest: dict[str, Any]
    artifacts: dict[str, Any]
    digests: dict[str, str]
    walked: dict[str, Path]
    verified: VerifiedPackage
    roles: list[RoleResult] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    dependencies: list[DependencyResult] = field(default_factory=list)
    evidence: list[PackageEvidence] = field(default_factory=list)
    topology: str | None = None
    source_key: str | None = None
    source_sha: str | None = None
    source_luid: str | None = None
    source_revision: str = REVISION_UNCONFIRMED
    published_key: str | None = None
    declared_dependencies: tuple[DeclaredDependency, ...] = ()
    model_role: str | None = None
    provider_ready: bool = False
    ordinal: int | None = None
    brief_policy: BriefPolicy | None = None
    spec_document: dict[str, Any] | None = None
    spec_member: pfs.HeldVerifiedMember | None = None
    spec_facts: PackageSpecFacts | None = None

    @property
    def keys(self) -> frozenset[str]:
        """Every S1-verified content key in this package."""
        return frozenset(self.walked)

    def under(self, prefix: str) -> list[str]:
        """Every verified content key inside ``prefix`` (a package-relative directory), sorted."""
        return sorted(key for key in self.walked if key.startswith(f"{prefix}/"))

    def json(self, key: str | None) -> Any:
        """A declared key's JSON payload, read from the path the WALK produced, or ``None``."""
        if key is None or key not in self.walked:
            return None
        if key == SPEC_NAME:
            return self.spec_document
        try:
            return pfs.parse_manifest_text(self.walked[key].read_text(encoding="utf-8"))
        except (OSError, ValueError, pfs._ManifestError):  # pylint: disable=protected-access
            self.blockers.append(CODE_IDENTITY_JSON)
            return None

    def text(self, key: str | None) -> str | None:
        """A declared key's text, read from the path the WALK produced, or ``None``."""
        if key is None or key not in self.walked:
            return None
        try:
            return self.walked[key].read_text(encoding="utf-8")
        except (OSError, ValueError):
            return None


def _blocked(
    unit: str | None,
    kind: str | None,
    code: str,
    *,
    roles: Sequence[RoleResult] = (),
) -> Phase1RoleIdentityResult:
    """A verdict for a package that cannot even be described - refused, never raised."""
    return Phase1RoleIdentityResult(
        verdict=VERDICT_BLOCKED,
        unit=unit,
        kind=kind if kind in (KIND_WORKBOOK, KIND_DATASOURCE) else None,  # type: ignore[arg-type]
        topology=None,
        roles=tuple(roles),
        blockers=(code,),
    )


class _IdentityError(Exception):
    """A typed identity refusal containing no artifact-controlled text."""


def _declared_string(mapping: Any, key: str) -> str | None:
    """Only absent/null claims are absent; a supplied claim of another type is invalid."""
    if not isinstance(mapping, dict):
        raise _IdentityError(CODE_IDENTITY_TYPE)
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _IdentityError(CODE_IDENTITY_TYPE)
    return value if value.strip() else None


def _object_rows(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(row, dict) for row in value)


def _luid(mapping: dict[str, Any], key: str) -> str | None:
    value = _declared_string(mapping, key)
    if value is not None and _filename_luid(f"{value}_") is None:
        raise _IdentityError(CODE_IDENTITY_TYPE)
    return value.casefold() if value is not None else None


def _facts(  # pylint: disable=too-many-return-statements
    root: Path, cleared: VerifiedPackage
) -> _Facts | Phase1RoleIdentityResult:
    """Read one package's own account of itself, or refuse before any role is considered."""
    if not cleared.classification.is_safe:
        return _blocked(cleared.classification.unit_name or None, None, CODE_UNSAFE_TARGET)
    if not cleared.classification.declares_self_contained:
        return _blocked(cleared.classification.unit_name or None, None, CODE_NOT_A_PACKAGE)
    if not cleared.integrity.is_clean:
        return _blocked(cleared.classification.unit_name or None, None, CODE_INTEGRITY_NOT_CLEAN)
    if not cleared.integrity.has_read_authority():
        return _blocked(cleared.classification.unit_name or None, None, CODE_INTEGRITY_CHANGED)

    held_manifest = cleared.integrity.manifest
    if (
        not cleared.is_bound_to(str(root))
        or held_manifest is None
        or held_manifest.root_identity != cleared.root_identity
        or cleared.integrity.root_identity != cleared.root_identity
    ):
        return _blocked(cleared.classification.unit_name or None, None, CODE_INTEGRITY_CHANGED)
    try:
        manifest = pfs.parse_manifest_text(held_manifest.content.decode("utf-8"))
    except (ValueError, pfs._ManifestError):  # pylint: disable=protected-access
        return _blocked(cleared.classification.unit_name or None, None, CODE_MANIFEST_UNREADABLE)
    digests = {member.relative_path: member.sha256 for member in cleared.integrity.verified_files}
    walked = {member.relative_path: member.path for member in cleared.integrity.verified_files}

    unit = _declared_string(manifest, "unit")
    if unit is None:
        return _blocked(None, None, CODE_UNIT_MISSING)
    kind = _declared_string(manifest, "kind")
    if kind not in (KIND_WORKBOOK, KIND_DATASOURCE):
        return _blocked(unit, None, CODE_KIND_UNCLASSIFIED)
    artifacts = manifest.get("artifacts", {})
    if not isinstance(artifacts, dict) or not pfs.is_canonical_key(unit) or "/" in unit:
        return _blocked(None, kind, CODE_IDENTITY_TYPE)
    return _facts_with_source(
        _Facts(
            root=root,
            unit=unit,
            kind=kind,
            manifest=manifest,
            artifacts=artifacts,
            digests=digests,
            walked=walked,
            verified=cleared,
        )
    )


def _facts_with_source(facts: _Facts) -> _Facts:
    """Fill in the source-role key, its verified digest, and the spec's published dependencies."""
    declared_asset = _declared_string(facts.artifacts, "asset")
    if declared_asset is not None and declared_asset in facts.digests:
        facts.source_key = declared_asset
        facts.source_sha = facts.digests[declared_asset]
    spec_key = _declared_string(facts.artifacts, "migration_spec")
    if spec_key == SPEC_NAME and spec_key in facts.digests:
        held = facts.verified.read_verified_member(facts.root, spec_key)
        if isinstance(held, pfs.PackageFilesystemResult):
            facts.blockers.extend(held.codes())
        else:
            try:
                facts.spec_document = pfs.parse_manifest_text(held.content.decode("utf-8"))
            except (ValueError, pfs._ManifestError):  # pylint: disable=protected-access
                facts.blockers.append(CODE_IDENTITY_JSON)
            else:
                facts.spec_member = held
                facts.spec_facts = package_spec_facts(facts.spec_document)
    spec = facts.json(spec_key)
    facts.declared_dependencies = _spec_dependencies(spec)
    if facts.kind == KIND_DATASOURCE:
        keys = {row.key for row in facts.declared_dependencies if row.key is not None}
        facts.blockers.extend(row.code for row in facts.declared_dependencies if row.code is not None)
        if len(keys) > 1:
            facts.blockers.append(CODE_PROVIDER_KEY_CONTRADICTION)
        elif keys:
            facts.published_key = next(iter(keys))
    return facts


def _spec_dependencies(spec: Any) -> tuple[DeclaredDependency, ...]:
    """Preserve every published row, including identity-less, malformed and duplicate rows."""
    if not isinstance(spec, dict):
        return ()
    rows = spec.get("data_sources")
    if not isinstance(rows, list):
        return (DeclaredDependency(code=CODE_DEPENDENCY_INVALID),)
    found: list[DeclaredDependency] = []
    for row in rows:
        if not isinstance(row, dict):
            found.append(DeclaredDependency(code=CODE_DEPENDENCY_INVALID))
            continue
        if "published_datasource" not in row:
            continue
        published = row["published_datasource"]
        try:
            luid = _luid(published, "luid")
            key = _declared_string(published, "key")
        except _IdentityError:
            found.append(DeclaredDependency(code=CODE_DEPENDENCY_INVALID))
            continue
        found.append(DeclaredDependency(luid, key, CODE_DEPENDENCY_IDENTITY if luid is None and key is None else None))
    return tuple(found)


# ---------------------------------------------------------------------------------------------
# role helpers
# ---------------------------------------------------------------------------------------------


def _role(role: str, state: str, cardinality: str, paths: Iterable[str] = (), code: str | None = None) -> RoleResult:
    return RoleResult(role=role, state=state, cardinality=cardinality, paths=tuple(sorted(paths)), code=code)


def _declared_role(  # pylint: disable=too-many-return-statements
    role: str,
    cardinality: str,
    declared: str | None,
    candidates: Sequence[str],
    verified: Iterable[str],
) -> RoleResult:
    """Adjudicate one DECLARED role against the verified candidates that could have filled it.

    The order is the invariant, and it is what makes rediscovery impossible:

    1. no declaration is ``missing`` **even when a candidate file is sitting right there** - the
       producer never said which file plays the role, and picking the only one present is exactly
       the fail-open that let a package with ``artifacts.asset`` deleted read as READY;
    2. a declaration naming something the walk did not verify is ``mismatch``;
    3. more than one admissible candidate is ``ambiguous`` - two source assets mean the package
       cannot say which bytes it is about;
    4. a declaration that is not among the admissible candidates (wrong extension, wrong directory)
       is ``mismatch``;
    5. only then ``resolved``.
    """
    if declared is None:
        return _role(role, STATE_MISSING, cardinality, candidates, CODE_ROLE_UNDECLARED)
    if declared not in set(verified):
        return _role(role, STATE_MISMATCH, cardinality, candidates, CODE_ROLE_NOT_VERIFIED)
    if len(candidates) > 1:
        return _role(role, STATE_AMBIGUOUS, cardinality, candidates, CODE_ROLE_AMBIGUOUS)
    if declared not in candidates:
        return _role(role, STATE_MISMATCH, cardinality, [declared], CODE_ROLE_WRONG_CANDIDATE)
    return _role(role, STATE_RESOLVED, cardinality, [declared])


def _fabric_directories(facts: _Facts, suffix: str) -> list[str]:
    """Every ``fabric/<Name><suffix>`` directory the verified key set proves exists."""
    found: set[str] = set()
    for key in facts.under("fabric"):
        head = key.split("/")[1] if key.count("/") >= 2 else ""
        if head.endswith(suffix):
            found.add(f"fabric/{head}")
    return sorted(found)


def _scope_unit(payload: Any) -> str | None:
    """A packaged artifact's own ``scope.unit`` stamp, or ``None`` when it does not carry one."""
    scope = payload.get("scope") if isinstance(payload, dict) else None
    return _declared_string(scope, "unit")


# ---------------------------------------------------------------------------------------------
# the individual role checks
# ---------------------------------------------------------------------------------------------


def _package_scope_role(facts: _Facts) -> RoleResult:
    """Unit and kind must agree with the engine's own classification of this unit, exactly once."""
    report = facts.json("report.json")
    if not isinstance(report, dict):
        return _role(ROLE_ENGINE_CLASSIFICATION, STATE_MISSING, "1 file", [], CODE_ROLE_MISSING)
    if _scope_unit(report) != facts.unit:
        return _role(ROLE_ENGINE_CLASSIFICATION, STATE_MISMATCH, "1 file", ["report.json"], CODE_SCOPE_MISMATCH)
    try:
        workbooks = _names(report.get("workbooks", []))
        datasources = _names(report.get("datasources", []))
    except _IdentityError:
        return _role(ROLE_ENGINE_CLASSIFICATION, STATE_MISMATCH, "1 row", ["report.json"], CODE_IDENTITY_TYPE)
    mine, theirs = (workbooks, datasources) if facts.kind == KIND_WORKBOOK else (datasources, workbooks)
    if mine.count(facts.unit) != 1 or facts.unit in theirs:
        return _role(ROLE_ENGINE_CLASSIFICATION, STATE_MISMATCH, "1 row", ["report.json"], CODE_ENGINE_MEMBERSHIP)
    return _role(ROLE_ENGINE_CLASSIFICATION, STATE_RESOLVED, "1 row", ["report.json"])


def _names(rows: Any) -> list[str]:
    if not _object_rows(rows) or any(not _declared_string(row, "name") for row in rows):
        raise _IdentityError(CODE_IDENTITY_TYPE)
    return [row["name"] for row in rows]


def _source_asset_role(facts: _Facts) -> RoleResult:
    """Exactly one source file, in ``assets/``, with an extension this package's KIND may carry."""
    extensions = SOURCE_EXTENSIONS[facts.kind]
    candidates = [key for key in facts.under("assets") if key.lower().endswith(extensions)]
    declared = _declared_string(facts.artifacts, "asset")
    result = _declared_role(ROLE_SOURCE_ASSET, "1 file", declared, candidates, facts.digests)
    if result.blocks:
        return result
    # An `assets/` directory holding a second, non-source file is not ambiguity about WHICH source
    # this is, but the container rule still applies: unrelated bytes belong in a declared data role.
    if len(facts.under("assets")) != 1:
        return _role(ROLE_SOURCE_ASSET, STATE_AMBIGUOUS, "1 file", facts.under("assets"), CODE_ROLE_AMBIGUOUS)
    return result


def _provenance_role(facts: _Facts) -> tuple[RoleResult, dict[str, Any] | None]:  # pylint: disable=too-many-return-statements
    """Exactly one provenance row, and it must be about the source role's own bytes."""
    payload = facts.json("source-provenance.json")
    if not isinstance(payload, dict):
        return _role(ROLE_SOURCE_PROVENANCE, STATE_MISSING, "1 file", [], CODE_ROLE_MISSING), None
    if _scope_unit(payload) != facts.unit:
        return (
            _role(ROLE_SOURCE_PROVENANCE, STATE_MISMATCH, "1 file", ["source-provenance.json"], CODE_SCOPE_MISMATCH),
            None,
        )
    rows = payload.get("inputs")
    if not _object_rows(rows):
        return _role(ROLE_SOURCE_PROVENANCE, STATE_MISMATCH, "1 row", [], CODE_IDENTITY_TYPE), None
    if len(rows) != 1:
        return (
            _role(
                ROLE_SOURCE_PROVENANCE,
                STATE_MISSING if not rows else STATE_AMBIGUOUS,
                "1 row",
                [],
                CODE_PROVENANCE_ROWS,
            ),
            None,
        )
    row = rows[0]
    supplied = row.get("input")
    if not isinstance(supplied, dict) or not isinstance(row.get("origin", {}), dict):
        return _role(ROLE_SOURCE_PROVENANCE, STATE_MISMATCH, "1 row", [], CODE_IDENTITY_TYPE), None
    if not _declared_string(supplied, "file") or not _declared_string(supplied, "sha256"):
        return _role(ROLE_SOURCE_PROVENANCE, STATE_MISMATCH, "1 row", [], CODE_IDENTITY_TYPE), None
    if facts.source_sha is None or supplied.get("sha256") != facts.source_sha:
        return (
            _role(ROLE_SOURCE_PROVENANCE, STATE_MISMATCH, "1 row", ["source-provenance.json"], CODE_PROVENANCE_SHA),
            None,
        )
    return _role(ROLE_SOURCE_PROVENANCE, STATE_RESOLVED, "1 row", ["source-provenance.json"]), row


def _source_identity_role(facts: _Facts, row: dict[str, Any] | None) -> RoleResult:
    """The same-byte cross-checks: provenance filename and spec filename name the source role."""
    if facts.source_key is None or row is None:
        return _role(ROLE_SOURCE_IDENTITY, STATE_MISSING, "1 sha256", [], CODE_SOURCE_SHA_MISSING)
    basename = PurePosixPath(facts.source_key).name
    supplied = row.get("input") if isinstance(row.get("input"), dict) else {}
    if supplied.get("file") != basename:
        return _role(ROLE_SOURCE_IDENTITY, STATE_MISMATCH, "1 sha256", [facts.source_key], CODE_PROVENANCE_FILE)
    spec = facts.json(_declared_string(facts.artifacts, "migration_spec"))
    source = spec.get("source", {}) if isinstance(spec, dict) else {}
    if not isinstance(source, dict):
        return _role(ROLE_SOURCE_IDENTITY, STATE_MISMATCH, "1 sha256", [facts.source_key], CODE_IDENTITY_TYPE)
    if _declared_string(source, "file_name") != basename:
        return _role(ROLE_SOURCE_IDENTITY, STATE_MISMATCH, "1 sha256", [facts.source_key], CODE_SPEC_FILE)
    return _role(ROLE_SOURCE_IDENTITY, STATE_RESOLVED, "1 sha256", [facts.source_key])


#: The two LUID namespaces, keyed by the kind that owns each. ⚠️ They are NEVER interchangeable: a
#: datasource LUID in a workbook's provenance is a category error, not a spelling difference, and
#: reading one as the other is how a name-free comparison silently compares nothing.
LUID_FIELD = {KIND_WORKBOOK: "workbook_luid", KIND_DATASOURCE: "datasource_luid"}


def _server_identity_role(facts: _Facts, row: dict[str, Any] | None) -> RoleResult:  # pylint: disable=too-many-return-statements
    """The Tableau Server LUID, when one exists - and an EARNED ``not_applicable`` when none does."""
    if row is None or facts.source_key is None:
        return _role(ROLE_SERVER_IDENTITY, STATE_MISSING, "0..1 luid", [], CODE_SOURCE_SHA_MISSING)
    origin = row.get("origin") if isinstance(row.get("origin"), dict) else {}
    mine = LUID_FIELD[facts.kind]
    theirs = LUID_FIELD[KIND_DATASOURCE if facts.kind == KIND_WORKBOOK else KIND_WORKBOOK]
    if _luid(origin, theirs) is not None:
        return _role(ROLE_SERVER_IDENTITY, STATE_MISMATCH, "0..1 luid", [], CODE_LUID_NAMESPACE)
    recorded = _luid(origin, mine)
    stamped = _filename_luid(PurePosixPath(facts.source_key).name)
    if facts.kind == KIND_DATASOURCE and any(
        dep.luid is not None and dep.luid != recorded for dep in facts.declared_dependencies
    ):
        return _role(ROLE_SERVER_IDENTITY, STATE_MISMATCH, "0..1 luid", [], CODE_LUID_CONTRADICTION)
    if recorded is None and stamped is None:
        return _role(ROLE_SERVER_IDENTITY, STATE_NOT_APPLICABLE, "0..1 luid", [], LIMITATION_LOCAL_SOURCE)
    if recorded is None:
        return _role(ROLE_SERVER_IDENTITY, STATE_MISMATCH, "0..1 luid", [], CODE_LUID_UNREPRESENTED)
    if stamped is not None and stamped.casefold() != recorded.casefold():
        return _role(ROLE_SERVER_IDENTITY, STATE_MISMATCH, "0..1 luid", [], CODE_LUID_CONTRADICTION)
    facts.source_luid = recorded
    return _role(ROLE_SERVER_IDENTITY, STATE_RESOLVED, "0..1 luid", [])


def _filename_luid(name: str) -> str | None:
    """The canonical-UUID prefix `harvest_estate_assets.py` writes, or ``None``.

    Deliberately a pure shape test on the packaged basename: it is a CROSS-CHECK against a recorded
    LUID, never an identity on its own, and it is compared only within the package's own namespace.
    """
    head = name.split("_", 1)[0]
    parts = head.split("-")
    if len(parts) != 5 or [len(part) for part in parts] != [8, 4, 4, 4, 12]:
        return None
    return head if all(char in "0123456789abcdefABCDEF" for part in parts for char in part) else None


def _receipt_role(facts: _Facts, required: Mapping[str, str]) -> RoleResult:  # pylint: disable=too-many-return-statements
    """The engine receipt must scope to this unit and ACCOUNT for every required fabric role.

    ``required`` maps role name to the package-relative path that role resolved to. Each one must be
    covered by at least one receipt output row, and every row must itself be a verified file in this
    package - a row naming another unit's output is what "this package was composed from two builds"
    looks like from the inside.
    """
    payload = facts.json("engine-output-receipt.json")
    if not isinstance(payload, dict):
        return _role(ROLE_ENGINE_RECEIPT, STATE_MISSING, "1 file", [], CODE_ROLE_MISSING)
    if _scope_unit(payload) != facts.unit:
        return _role(ROLE_ENGINE_RECEIPT, STATE_MISMATCH, "1 file", ["engine-output-receipt.json"], CODE_RECEIPT_SCOPE)
    rows = payload.get("artifacts")
    if not _object_rows(rows) or any(not _declared_string(row, "path") for row in rows):
        return _role(ROLE_ENGINE_RECEIPT, STATE_MISMATCH, "1..N outputs", [], CODE_IDENTITY_TYPE)
    outputs = [row["path"] for row in rows]
    if not outputs:
        return _role(ROLE_ENGINE_RECEIPT, STATE_MISSING, "1..N outputs", [], CODE_RECEIPT_UNCOVERED)
    foreign = sorted(path for path in outputs if path not in facts.digests)
    if foreign:
        return _role(
            ROLE_ENGINE_RECEIPT,
            STATE_MISMATCH,
            "1..N outputs",
            ["engine-output-receipt.json"],
            CODE_RECEIPT_FOREIGN_OUTPUT,
        )
    for role_path in sorted(set(required.values())):
        prefix = role_path if role_path.endswith((".Report", ".SemanticModel")) else None
        covered = any(path == role_path or (prefix and path.startswith(f"{prefix}/")) for path in outputs)
        if not covered:
            return _role(ROLE_ENGINE_RECEIPT, STATE_MISMATCH, "1..N outputs", [role_path], CODE_RECEIPT_UNCOVERED)
    return _role(ROLE_ENGINE_RECEIPT, STATE_RESOLVED, "1..N outputs", ["engine-output-receipt.json"])


def _handover_role(facts: _Facts) -> RoleResult:
    """A workbook's handover slice, scoped to this unit by the package's OWN stamp.

    ⚠️ ``workbook.source_id`` is deliberately not read. It is the diagnostic the packager resolves an
    asset by, and using it here would make a package whose asset role was deleted resolvable again
    from a second channel - the precise rediscovery this slice refuses.
    """
    declared = _declared_string(facts.artifacts, "handover")
    candidates = facts.under("handover")
    result = _declared_role(ROLE_HANDOVER, "1 file", declared, candidates, facts.digests)
    if result.blocks:
        return result
    if _scope_unit(facts.json(declared)) != facts.unit:
        return _role(ROLE_HANDOVER, STATE_MISMATCH, "1 file", [declared or "handover"], CODE_SCOPE_MISMATCH)
    return result


def parse_brief_policy(  # pylint: disable=too-many-return-statements
    text: str, unit: str, scope: str
) -> tuple[str | None, BriefPolicy | None]:
    """One TOML parser for preparation and S2: strict policy, or truthful legacy non-policy.

    A header without fallback authorization may carry only the legacy identity keys. An explicit
    policy requires all four string fields; malformed/unknown fields refuse without echoing text.
    """
    env = tableau_env.resolve_env(_ENV_PATH)
    scrub = tableau_env.env_redactor(env, *(env.get(key, "") for key in sorted(tableau_env.DATASOURCE_CREDENTIAL_KEYS)))
    if discloses_host_location(text) or scrub(text) != text:
        return CODE_BRIEF_UNSAFE, None
    lines = text.replace("\r\n", "\n").split("\n")
    first = next((line for line in lines if line.strip()), "")
    if "+++" not in first:
        return None, None
    closing = next((index for index in range(1, len(lines)) if lines[index] == "+++"), None)
    if lines[0] != "+++" or closing is None or any("+++" in line for line in lines[closing + 1 :]):
        return CODE_BRIEF_FRONTMATTER, None
    try:
        front = tomllib.loads("\n".join(lines[1:closing]))
    except (tomllib.TOMLDecodeError, ValueError):
        return CODE_BRIEF_FRONTMATTER, None
    if front.get("unit") != unit:
        return CODE_BRIEF_UNIT, None
    supplied_scope = front.get("scope")
    if supplied_scope is not None and supplied_scope != scope:
        return CODE_BRIEF_SCOPE, None
    allowed = {"schema", "unit", "scope", "fallback_authorization"}
    if (
        set(front) - allowed
        or any(not isinstance(value, str) for value in front.values())
        or ("schema" in front and front["schema"] != "phase1-start-ready/v1")
    ):
        return CODE_BRIEF_POLICY, None
    if "fallback_authorization" not in front:
        return None, None
    if set(front) != allowed or front["fallback_authorization"] not in ("stop", "model_only_unvalidated"):
        return CODE_BRIEF_POLICY, None
    return None, BriefPolicy(supplied_scope, front["fallback_authorization"])


def brief_identity(text: str, unit: str, scope: str) -> tuple[str | None, bool]:
    """Compatibility handoff: identity/policy refusal and whether policy remains unparsed."""
    code, policy = parse_brief_policy(text, unit, scope)
    return code, code is None and policy is None


def _brief_role(facts: _Facts, topology: str) -> tuple[RoleResult, list[str]]:
    """The declared brief's bytes must be safe to ship and identify this unit and topology."""
    declared = _declared_string(facts.artifacts, "migration_brief")
    candidates = [BRIEF_NAME] if BRIEF_NAME in facts.digests else []
    result = _declared_role(ROLE_MIGRATION_BRIEF, "1 file", declared, candidates, facts.digests)
    if result.blocks:
        return result, [LIMITATION_BRIEF_POLICY_UNPARSED]
    text = facts.text(declared)
    if text is None:
        return _role(ROLE_MIGRATION_BRIEF, STATE_MISMATCH, "1 file", [BRIEF_NAME], CODE_ROLE_NOT_VERIFIED), []
    code, policy = parse_brief_policy(text, facts.unit, TOPOLOGY_SCOPE[topology])
    if code is not None:
        return _role(ROLE_MIGRATION_BRIEF, STATE_MISMATCH, "1 file", [BRIEF_NAME], code), []
    facts.brief_policy = policy
    return result, [LIMITATION_BRIEF_POLICY_UNPARSED] if policy is None else []


def _evidence_role(  # pylint: disable=too-many-return-statements
    facts: _Facts, role: str, directory: str, manifest_name: str
) -> RoleResult:
    """A 0..1 evidence directory: one manifest, every file accounted for, owned by THIS source.

    Ownership is by the only stable axis each provider has - the reference manifest carries the
    workbook's source SHA, and every oracle view carries the workbook LUID. A file inside the
    directory that no record names is refused rather than ignored: unattributed bytes beside real
    evidence is how a foreign render gets read as this unit's.
    """
    keys = facts.under(directory)
    declared = _declared_string(facts.artifacts, "reference" if role == ROLE_TABLEAU_REFERENCE else "oracle")
    if declared is not None and (declared != directory or not keys):
        return _role(role, STATE_MISMATCH, "0..1 dir", keys, CODE_ROLE_NOT_VERIFIED)
    if not keys:
        return _role(role, STATE_NOT_APPLICABLE, "0..1 dir", [])
    manifest_key = f"{directory}/{manifest_name}"
    if manifest_key not in facts.digests:
        return _role(role, STATE_MISSING, "1 manifest", keys, CODE_ROLE_MISSING)
    payload = facts.json(manifest_key)
    if not isinstance(payload, dict):
        return _role(role, STATE_MISMATCH, "1 manifest", [manifest_key], CODE_EVIDENCE_MANIFEST)
    try:
        records, named, claims = (
            _reference_records(facts, payload, directory)
            if role == ROLE_TABLEAU_REFERENCE
            else _oracle_records(facts, payload, directory)
        )
    except _IdentityError as exc:
        return _role(role, STATE_MISMATCH, "1 manifest", [manifest_key], str(exc))
    unaccounted = sorted(set(keys) - named - {manifest_key})
    if unaccounted:
        return _role(role, STATE_MISMATCH, "0..N files", unaccounted, CODE_EVIDENCE_UNACCOUNTED)
    if not records:
        # A manifest declaring no record is an EMPTY capture, not evidence: it owns nothing, so it
        # cannot be a visual-evidence provider and it cannot be foreign either.
        return _role(role, STATE_NOT_APPLICABLE, "0 records", [manifest_key])
    if role == ROLE_TABLEAU_REFERENCE:
        owner = _declared_string(payload, "source_workbook_sha256")
        if owner is None or facts.source_sha is None or owner.casefold() != facts.source_sha.casefold():
            return _role(role, STATE_MISMATCH, "1 manifest", [manifest_key], CODE_EVIDENCE_FOREIGN)
    mine = {facts.source_luid} if facts.source_luid else set()
    # ⚠️ The two providers are held to different rules because they carry different identities. A
    # reference capture is owned by the source SHA, which is checked above, so a LUID it does not
    # state cannot contradict anything - only a STATED foreign one can. An oracle record has no SHA
    # at all: `workbook_luid` is the whole of its ownership claim, so a view that states none is
    # unattributed rather than harmlessly quiet.
    if any((claim - mine if role == ROLE_TABLEAU_REFERENCE else claim ^ mine) for claim in claims):
        return _role(role, STATE_MISMATCH, "1 owner", [manifest_key], CODE_EVIDENCE_FOREIGN)
    facts.evidence.extend(records)
    return _role(role, STATE_RESOLVED, "1 dir", [manifest_key])


def _evidence_path(facts: _Facts, directory: str, value: Any) -> tuple[str, Path]:
    """Accept a canonical role-relative spelling only when its exact key was walked by S1."""
    if not isinstance(value, str) or not pfs.is_canonical_key(value):
        raise _IdentityError(CODE_EVIDENCE_PATH)
    key = f"{directory}/{value}"
    if key not in facts.walked:
        raise _IdentityError(CODE_EVIDENCE_PATH)
    return key, facts.walked[key]


def _evidence_claims(*mappings: dict[str, Any]) -> set[str]:
    return {
        claim
        for mapping in mappings
        for key in ("workbook_luid", "source_workbook_luid")
        if (claim := _luid(mapping, key)) is not None
    }


def _render_types(record: dict[str, Any]) -> None:
    """Check path/digest claim types, leaving dimension formats and grades to the existing reader."""
    for key in ("provider", "sha256", "path", "retained_path", "image", "view_type", "object_type"):
        _declared_string(record, key)
    for value in (record.get("bytes"),):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise _IdentityError(CODE_IDENTITY_TYPE)
    if "capabilities" in record and (
        not isinstance(record["capabilities"], list) or any(not isinstance(cap, str) for cap in record["capabilities"])
    ):
        raise _IdentityError(CODE_IDENTITY_TYPE)


def _reference_records(
    facts: _Facts, payload: dict[str, Any], directory: str
) -> tuple[list[PackageEvidence], set[str], list[set[str]]]:
    """Every reference state, every ownership claim, and only walk-produced image paths."""
    records: list[PackageEvidence] = []
    named: set[str] = set()
    claims: list[set[str]] = []
    entries = payload.get("dashboards", [])
    if not _object_rows(entries):
        raise _IdentityError(CODE_IDENTITY_TYPE)
    for entry in entries:
        _declared_string(entry, "name")
        states = entry.get("states", [])
        if not _object_rows(states):
            raise _IdentityError(CODE_IDENTITY_TYPE)
        for state in states:
            _render_types(state)
            claims.append(_evidence_claims(payload, entry, state))
            key, path = _evidence_path(facts, directory, state.get("image"))
            named.add(key)
            records.append(PackageEvidence("reference", payload, entry, state, path))
    return records, named, claims


def _oracle_paths(facts: _Facts, view: dict[str, Any], directory: str) -> tuple[dict[str, Any], Path | None, set[str]]:
    """Check all four leg paths; choose a render only from the already-walked candidates."""
    named: set[str] = set()
    selected: dict[str, Any] = {}
    selected_path = None
    for leg in ("image", "svg", "pdf", "data"):
        payload_leg = view.get(leg)
        if payload_leg is None:
            continue
        if not isinstance(payload_leg, dict):
            raise _IdentityError(CODE_IDENTITY_TYPE)
        _render_types(payload_leg)
        for field_name in ("path", "retained_path"):
            value = payload_leg.get(field_name)
            if value is None:
                continue
            key, path = _evidence_path(facts, directory, value)
            named.add(key)
            if field_name == "path" and leg != "data" and payload_leg.get("status") == "ok" and selected_path is None:
                selected, selected_path = payload_leg, path
    return selected, selected_path, named


def _oracle_records(
    facts: _Facts, payload: dict[str, Any], directory: str
) -> tuple[list[PackageEvidence], set[str], list[set[str]]]:
    """Every oracle row and declared leg path, including retained and non-render data legs."""
    records: list[PackageEvidence] = []
    named: set[str] = set()
    claims: list[set[str]] = []
    views = payload.get("views", [])
    if not _object_rows(views):
        raise _IdentityError(CODE_IDENTITY_TYPE)
    for view in views:
        for key in ("view_name", "view_url_name", "view_type", "workbook_name"):
            _declared_string(view, key)
        claim = _evidence_claims(payload, view)
        if _luid(view, "workbook_luid") is None:
            raise _IdentityError(CODE_EVIDENCE_FOREIGN)
        claims.append(claim)
        selected, selected_path, paths = _oracle_paths(facts, view, directory)
        named.update(paths)
        records.append(PackageEvidence("oracle", payload, view, selected, selected_path))
    return records, named, claims


# ---------------------------------------------------------------------------------------------
# topology and the cohort edge
# ---------------------------------------------------------------------------------------------


def _topology(facts: _Facts) -> str:
    """What SHAPE this package is - decided from the engine's kind plus the spec's own dependencies.

    A datasource package that also emits a self-service report is still a datasource: kind comes from
    the engine's classification, never from the filesystem, so an auxiliary `.Report` cannot promote
    it into a workbook.
    """
    if facts.kind == KIND_WORKBOOK:
        return TOPOLOGY_PUBLISHED_CONSUMER if facts.declared_dependencies else TOPOLOGY_OWNED_MODEL
    return TOPOLOGY_STANDALONE_DATASOURCE


def _provider_matches(dependency: DeclaredDependency, providers: Sequence[_Facts]) -> tuple[list[_Facts], list[_Facts]]:
    """`(matched by datasource LUID, matched by exact published key)` across the cohort.

    Both axes are computed even though only one may be USED, because the disagreement between them
    is itself a finding: a package that answers the exact `<site>/<name>` key while carrying a
    different datasource LUID is not a near-miss provider, it is a contradiction.
    """
    luid, key = dependency.luid, dependency.key
    by_luid = [p for p in providers if luid and p.source_luid and p.source_luid.casefold() == luid.casefold()]
    by_key = [p for p in providers if key is not None and p.published_key == key]
    return by_luid, by_key


def _binding_matches(facts: _Facts, provider: _Facts) -> bool:
    """Compare the strict walked PBIR and summary with the provider's complete resolved model path.

    Normalisation is lexical: it compares addresses without opening or following the binding target.
    Only the provider's already-resolved declared model may satisfy the comparison.
    """
    report_role = next((row for row in facts.roles if row.role == ROLE_FABRIC_REPORT), None)
    if report_role is None or report_role.state != STATE_RESOLVED or provider.model_role is None:
        return False
    pbir_key = f"{report_role.paths[0]}/definition.pbir"
    payload = facts.json(pbir_key)
    summary = facts.manifest.get("model_binding")
    if not isinstance(payload, dict) or not isinstance(summary, dict):
        return False
    reference = payload.get("datasetReference")
    by_path = reference.get("byPath") if isinstance(reference, dict) else None
    if not isinstance(reference, dict) or set(reference) != {"byPath"} or not isinstance(by_path, dict):
        return False
    declared = _declared_string(by_path, "path")
    if (
        not declared
        or "\\" in declared
        or declared.startswith("/")
        or any(part not in (".", "..") and not pfs.is_canonical_key(part) for part in declared.split("/"))
    ):
        return False
    if (
        summary.get("kind") != "byPath"
        or summary.get("path") != declared
        or not isinstance(summary.get("resolves_in_package"), bool)
    ):
        return False
    actual = os.path.normcase(os.path.abspath(facts.walked[pbir_key].parent / declared))
    expected = os.path.normcase(os.path.abspath(provider.root / provider.model_role))
    local = os.path.normcase(os.path.abspath(facts.root)) == os.path.normcase(os.path.abspath(provider.root))
    return actual == expected and summary["resolves_in_package"] == local


def _dependency(  # pylint: disable=too-many-return-statements
    facts: _Facts, dependency: DeclaredDependency, providers: Sequence[_Facts]
) -> DependencyResult:
    """Resolve one published edge to exactly one provider package, and check the report binds to it."""
    luid, key = dependency.luid, dependency.key
    if dependency.code is not None:
        return DependencyResult(STATE_MISMATCH, luid, key, code=dependency.code)
    by_luid, by_key = _provider_matches(dependency, providers)
    if luid is not None and not by_luid and by_key:
        named = by_key[0].unit if len(by_key) == 1 else None
        return DependencyResult(STATE_MISMATCH, luid, key, named, code=CODE_PROVIDER_LUID_CONTRADICTION)
    matches = by_luid if luid is not None else [provider for provider in by_key if provider.source_luid is None]
    if luid is None and not matches and by_key:
        return DependencyResult(STATE_MISMATCH, luid, key, code=CODE_PROVIDER_LUID_CONTRADICTION)
    if not matches:
        return DependencyResult(STATE_MISSING, luid, key, code=CODE_PROVIDER_MISSING)
    if len(matches) > 1:
        return DependencyResult(STATE_AMBIGUOUS, luid, key, code=CODE_PROVIDER_AMBIGUOUS)
    provider = matches[0]
    if key is not None and provider.published_key is not None and provider.published_key != key:
        return DependencyResult(STATE_MISMATCH, luid, key, provider.unit, code=CODE_PROVIDER_KEY_CONTRADICTION)
    provider.topology = TOPOLOGY_PUBLISHED_PROVIDER
    if provider.model_role is None:
        return DependencyResult(STATE_MISSING, luid, key, provider.unit, code=CODE_PROVIDER_MODEL)
    if not provider.provider_ready:
        return DependencyResult(STATE_MISMATCH, luid, key, provider.unit, code=CODE_PROVIDER_BLOCKED)
    if not _binding_matches(facts, provider):
        return DependencyResult(STATE_MISMATCH, luid, key, provider.unit, provider.model_role, CODE_PROVIDER_BINDING)
    return DependencyResult(
        STATE_RESOLVED, luid, key, provider.unit, provider.model_role, provider_ordinal=provider.ordinal
    )


# ---------------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------------


def verify_phase1_role_identity(
    package_roots: Sequence[Path], *, verified: Sequence[VerifiedPackage] | None = None
) -> tuple[Phase1RoleIdentityResult, ...]:
    """One typed verdict per root, with exact binding before any cohort role read.

    Earlier ``verified`` observations must biject, but never replace fresh no-follow S1.
    Bad packages return BLOCKED, not tracebacks carrying host paths or artifact-controlled text.
    """
    identities = tuple(str(root) for root in package_roots)
    if not unique_root_identities(identities) or (
        verified is not None and bind_root_results(identities, verified, verified_root_binding) is None
    ):
        return tuple(_blocked(None, None, CODE_ROOT_BINDING_INVALID) for _ in package_roots)
    clearances = bind_root_results(identities, tuple(verify_s1(root) for root in package_roots), verified_root_binding)
    if clearances is None:
        return tuple(_blocked(None, None, CODE_ROOT_BINDING_INVALID) for _ in package_roots)
    facts: list[_Facts] = []
    results: dict[int, Phase1RoleIdentityResult] = {}
    for index, (root, identity) in enumerate(zip(package_roots, identities, strict=True)):
        clearance = clearances[identity]
        try:
            outcome = _facts(root, clearance)
        except _IdentityError as exc:
            outcome = _blocked(clearance.classification.unit_name or None, None, str(exc))
        if isinstance(outcome, Phase1RoleIdentityResult):
            results[index] = replace(outcome, verified=clearance)
            continue
        facts.append(replace(outcome, ordinal=index))
    _resolve_cohort(facts)
    ordered: list[Phase1RoleIdentityResult] = []
    position = 0
    for index in range(len(package_roots)):
        if index in results:
            ordered.append(results[index])
            continue
        ordered.append(_verdict(facts[position]))
        position += 1
    return tuple(ordered)


def _resolve_cohort(facts: Sequence[_Facts]) -> None:
    """Assess every local role before any consumer may resolve against a provider."""
    providers = [entry for entry in facts if entry.kind == KIND_DATASOURCE]
    consumers = [entry for entry in facts if entry.kind == KIND_WORKBOOK and entry.declared_dependencies]
    for entry in facts:
        entry.topology = _topology(entry)
        try:
            _identify(entry)
            _assess_roles(entry)
        except _IdentityError as exc:
            entry.blockers.append(str(exc))
        entry.provider_ready = not entry.blockers and not any(role.blocks for role in entry.roles)
    for consumer in consumers:
        for dependency in consumer.declared_dependencies:
            try:
                row = _dependency(consumer, dependency, providers)
            except _IdentityError as exc:
                row = DependencyResult(STATE_MISMATCH, dependency.luid, dependency.key, code=str(exc))
            consumer.dependencies.append(row)


def _identify(facts: _Facts) -> None:
    """Establish the source SHA/LUID/published key BEFORE any cohort edge is resolved.

    Order matters: a provider's LUID is what a consumer's dependency matches on, so it has to exist
    before matching, and it may only exist if this package's own provenance/asset/filename agree.
    """
    _role_result, row = _provenance_role(facts)
    if row is not None:
        facts.source_revision = revision_status(row.get("origin", {}), [row])
    facts.roles.append(_role_result)
    facts.roles.append(_source_identity_role(facts, row))
    facts.roles.append(_server_identity_role(facts, row))


def _required_fabric(facts: _Facts, topology: str) -> tuple[list[RoleResult], dict[str, str]]:
    """The report/model/PBIP roles for this topology, plus the paths a receipt must account for."""
    reports = _fabric_directories(facts, ".Report")
    models = _fabric_directories(facts, ".SemanticModel")
    pbip = sorted(key for key in facts.under("fabric") if key.count("/") == 1 and key.lower().endswith(".pbip"))
    declared_report = _declared_string(facts.artifacts, "report")
    declared_model = _declared_string(facts.artifacts, "model")
    roles: list[RoleResult] = []
    required: dict[str, str] = {}

    consumer = topology == TOPOLOGY_PUBLISHED_CONSUMER
    datasource = topology in (TOPOLOGY_STANDALONE_DATASOURCE, TOPOLOGY_PUBLISHED_PROVIDER)
    if datasource and not reports and declared_report is None:
        roles.append(_role(ROLE_FABRIC_REPORT, STATE_NOT_APPLICABLE, "0..1 dir", []))
    else:
        report_role = _declared_role(ROLE_FABRIC_REPORT, "1 dir", declared_report, reports, reports)
        roles.append(report_role)
        if report_role.state == STATE_RESOLVED and declared_report:
            required[ROLE_FABRIC_REPORT] = declared_report
    if consumer:
        # A consumer REUSES the provider's model. A second copy is a mismatch, not a convenience:
        # two models for one report is exactly the drift that migrating a shared datasource once
        # exists to prevent, and nothing downstream can then say which one the numbers came from.
        owned = bool(models) or declared_model is not None
        roles.append(
            _role(ROLE_FABRIC_MODEL, STATE_MISMATCH, "0 dir", models, CODE_ROLE_WRONG_CANDIDATE)
            if owned
            else _role(ROLE_FABRIC_MODEL, STATE_NOT_APPLICABLE, "0 dir", [])
        )
    else:
        model_role = _declared_role(ROLE_FABRIC_MODEL, "1 dir", declared_model, models, models)
        roles.append(model_role)
        if model_role.state == STATE_RESOLVED and declared_model:
            required[ROLE_FABRIC_MODEL] = declared_model
            facts.model_role = declared_model
    if datasource and not pbip:
        roles.append(_role(ROLE_PBIP_ENTRYPOINT, STATE_NOT_APPLICABLE, "0..1 file", []))
    elif len(pbip) == 1:
        roles.append(_role(ROLE_PBIP_ENTRYPOINT, STATE_RESOLVED, "1 file", pbip))
        required[ROLE_PBIP_ENTRYPOINT] = pbip[0]
    else:
        roles.append(
            _role(
                ROLE_PBIP_ENTRYPOINT,
                STATE_AMBIGUOUS if pbip else STATE_MISSING,
                "1 file",
                pbip,
                CODE_ROLE_AMBIGUOUS if pbip else CODE_ROLE_MISSING,
            )
        )
    return roles, required


def _assess_roles(facts: _Facts) -> None:
    """Finish local roles once, before computing provider eligibility or published edges."""
    topology = facts.topology or _topology(facts)
    facts.json(_declared_string(facts.artifacts, "migration_spec_schema"))
    roles: list[RoleResult] = [
        _package_scope_role(facts),
        _source_asset_role(facts),
        *facts.roles,
        _declared_role(
            ROLE_MIGRATION_SPEC,
            "1 file",
            _declared_string(facts.artifacts, "migration_spec"),
            ["migration-spec.json"] if "migration-spec.json" in facts.digests else [],
            facts.digests,
        ),
        _declared_role(
            ROLE_MIGRATION_SPEC_SCHEMA,
            "1 file",
            _declared_string(facts.artifacts, "migration_spec_schema"),
            ["migration-spec.schema.json"] if "migration-spec.schema.json" in facts.digests else [],
            facts.digests,
        ),
    ]
    brief_role, limitations = _brief_role(facts, topology)
    roles.append(brief_role)
    fabric_roles, required = _required_fabric(facts, topology)
    roles.extend(fabric_roles)
    roles.append(_receipt_role(facts, required))
    roles.extend(_evidence_and_handover_roles(facts, topology))
    limitations.extend(
        role.code for role in roles if role.state == STATE_NOT_APPLICABLE and role.code == LIMITATION_LOCAL_SOURCE
    )
    facts.roles = roles
    facts.limitations = limitations
    if topology != TOPOLOGY_PUBLISHED_CONSUMER and any(
        row.role == ROLE_FABRIC_REPORT and row.state == STATE_RESOLVED for row in roles
    ):
        if not _binding_matches(facts, facts):
            facts.blockers.append(CODE_PROVIDER_BINDING)


def _verdict(facts: _Facts) -> Phase1RoleIdentityResult:
    """Fold assessed roles and dependencies, then contextualize the already-derived source facts."""
    topology = facts.topology or _topology(facts)
    roles = list(facts.roles)
    for row in facts.dependencies:
        roles.append(
            _role(
                ROLE_PUBLISHED_DEPENDENCY,
                row.state,
                "1 provider",
                [row.provider_unit] if row.provider_unit else [],
                row.code,
            )
        )
    if topology == TOPOLOGY_PUBLISHED_CONSUMER and not facts.dependencies:
        roles.append(_role(ROLE_PUBLISHED_DEPENDENCY, STATE_MISSING, "1 provider", [], CODE_PROVIDER_MISSING))
    blockers = [*facts.blockers, *(role.code or role.state for role in roles if role.blocks)]
    spec_facts = facts.spec_facts
    if spec_facts is not None:
        sources = facts.spec_document.get("data_sources") if isinstance(facts.spec_document, Mapping) else None
        # Self-publication metadata is not a proxy leg; only applicability depends on cohort topology.
        spec_facts = spec_facts._replace(
            direct_applicable=(
                topology in (TOPOLOGY_OWNED_MODEL, TOPOLOGY_STANDALONE_DATASOURCE, TOPOLOGY_PUBLISHED_PROVIDER)
                and spec_facts.refusal_code is None
                and isinstance(sources, list)
                and all(
                    isinstance(row, Mapping)
                    and isinstance(row.get("connection", {}), Mapping)
                    and row.get("connection", {}).get("class") != "sqlproxy"
                    for row in sources
                )
            ),
            published_only=topology == TOPOLOGY_PUBLISHED_CONSUMER and spec_facts.published_only,
        )
    result = Phase1RoleIdentityResult(
        verdict=VERDICT_BLOCKED if blockers else VERDICT_START_READY,
        unit=facts.unit,
        kind=facts.kind,  # type: ignore[arg-type]
        topology=topology,  # type: ignore[arg-type]
        roles=tuple(roles),
        source_identity=SourceIdentity(
            kind=facts.kind,  # type: ignore[arg-type]
            sha256=facts.source_sha,
            tableau_luid=facts.source_luid,
            published_key=facts.published_key,
            revision=facts.source_revision,
        ),
        dependencies=tuple(facts.dependencies),
        blockers=tuple(dict.fromkeys(blockers)),
        authorized_limitations=tuple(dict.fromkeys(facts.limitations)),
        evidence=tuple(facts.evidence) if not blockers else (),
        verified=facts.verified,
        brief_policy=facts.brief_policy,
        _data_access_snapshot=(
            _DataAccessSnapshot(
                facts.verified.integrity,
                facts.spec_member,
                spec_facts,
                facts.artifacts.get("data_access") == DATA_ACCESS_NAME,
            )
            if facts.spec_member is not None and spec_facts is not None
            else None
        ),
    )
    snapshot = result._data_access_snapshot  # pylint: disable=protected-access
    if result.is_start_ready and snapshot is not None:
        owner = weakref.ref(result)
        state = _handoff_authority_state(result)
        source_facts = snapshot.facts
        verified = result.verified
        issued_integrity = verified.integrity
        object.__setattr__(
            result,
            "_authority",
            lambda candidate: (
                owner() is candidate
                and candidate.verified is verified
                and candidate._data_access_snapshot is snapshot  # pylint: disable=protected-access
                and candidate.verified.integrity is issued_integrity
                and snapshot.integrity is issued_integrity
                and issued_integrity.has_read_authority()
                and snapshot.facts is source_facts
                and _handoff_authority_state(candidate) == state
            ),
        )
    return result


def _evidence_and_handover_roles(facts: _Facts, topology: str) -> list[RoleResult]:
    """Handover and the Tableau evidence roles - all four EARNED N/A for a datasource package."""
    if topology in (TOPOLOGY_STANDALONE_DATASOURCE, TOPOLOGY_PUBLISHED_PROVIDER):
        roles = []
        for name, declaration, prefixes in (
            (ROLE_HANDOVER, "handover", ("handover",)),
            (ROLE_TABLEAU_REFERENCE, "reference", ("reference",)),
            (ROLE_TABLEAU_ORACLE, "oracle", ORACLE_DIRECTORIES),
        ):
            paths = [key for prefix in prefixes for key in facts.under(prefix)]
            present = facts.artifacts.get(declaration) is not None or bool(paths)
            if name == ROLE_TABLEAU_ORACLE:
                oracle = facts.manifest.get("oracle", {})
                present = present or not isinstance(oracle, dict) or oracle.get("objects") not in (None, [])
            roles.append(
                _role(
                    name,
                    STATE_MISMATCH if present else STATE_NOT_APPLICABLE,
                    "0 files",
                    paths,
                    CODE_INAPPLICABLE_PRESENT if present else None,
                )
            )
        foreign_evidence = any(row.blocks for row in roles if row.role != ROLE_HANDOVER)
        roles.append(
            _role(
                ROLE_VISUAL_EVIDENCE,
                STATE_MISMATCH if foreign_evidence else STATE_NOT_APPLICABLE,
                "0 providers",
                [],
                CODE_INAPPLICABLE_PRESENT if foreign_evidence else None,
            )
        )
        return roles
    reference = _evidence_role(facts, ROLE_TABLEAU_REFERENCE, "reference", "manifest.json")
    oracle = _oracle_role(facts)
    resolved = [row for row in (reference, oracle) if row.state == STATE_RESOLVED]
    evidence = (
        _role(ROLE_VISUAL_EVIDENCE, STATE_RESOLVED, "1..2 providers", [row.role for row in resolved])
        if resolved
        else _role(ROLE_VISUAL_EVIDENCE, STATE_MISSING, "1..2 providers", [], CODE_ROLE_MISSING)
    )
    return [_handover_role(facts), reference, oracle, evidence]


#: The two directory names a packaged Tableau capture is written under. ``oracle/`` is what
#: `package_unit.py` writes; ``_oracle/`` is the flat capture `capture_tableau_oracle.py` produces
#: and is the spelling `check_reference_readiness._collect_evidence` also accepts. Both are read
#: here for the same reason that gate reads both - and a package carrying BOTH is ambiguous rather
#: than merged, because nothing says which capture the unit's evidence is.
ORACLE_DIRECTORIES = ("oracle", "_oracle")


def _oracle_role(facts: _Facts) -> RoleResult:
    """This package's oracle capture, in whichever of its two directory spellings it was written."""
    present = [name for name in ORACLE_DIRECTORIES if facts.under(name)]
    if len(present) > 1:
        return _role(ROLE_TABLEAU_ORACLE, STATE_AMBIGUOUS, "0..1 dir", present, CODE_ROLE_AMBIGUOUS)
    if not present and facts.artifacts.get("oracle") is None:
        return _role(ROLE_TABLEAU_ORACLE, STATE_NOT_APPLICABLE, "0..1 dir", [])
    if not present:
        return _role(ROLE_TABLEAU_ORACLE, STATE_MISMATCH, "0..1 dir", [], CODE_ROLE_NOT_VERIFIED)
    return _evidence_role(facts, ROLE_TABLEAU_ORACLE, present[0], "oracle-manifest.json")
