"""
purpose: prove a COHORT of handover packages carries every required semantic role, and that every
         stable identity claim those roles make agrees.
usage:   import package_role_identity as pri
         pri.verify_phase1_role_identity([Path("packages/Unit"), Path("packages/Shared")])

⚠️ **This is the roles-and-identity slice of issue #562 (S2), and nothing else.** It runs AFTER
`bundle_corpus.classify_target` (the boundary) and AFTER `package_filesystem.verify_package` (the
bytes), and it answers one question:

    does this package carry exactly the roles its kind and topology require, and do the identity
    claims those roles make - source SHA, Tableau LUID, unit scope, engine output, evidence
    ownership, published-provider edge - all agree?

It deliberately does **not** return a source ``Path``, search for a source, interpret credentials,
grade evidence, touch the working/dispatched lifecycle, or write anything into a package. Source
return is issue #558; the credential projection, the brief POLICY parser and the final
``START_READY`` fold are separate slices with separate invariants.

Three properties are the point, and each is structural rather than promised
--------------------------------------------------------------------------
1. **S1 is consumed, never assumed.** :func:`verify_phase1_role_identity` runs
   :func:`verify_s1` itself for every root it is given. A caller MAY hand in a
   :class:`VerifiedPackage`, but that object carries the root it was verified against, so a
   clearance computed for one path can never be applied to another.
2. **A role is a DECLARATION, never a discovery.** Every role is read from
   ``package-manifest.json``'s ``artifacts`` map (or, for the roles the manifest does not declare,
   from the S1-verified content key set) and then *confirmed* against the verified bytes. Removing
   ``artifacts.asset`` while the file remains is ``missing`` - the file is NOT rediscovered by
   scanning ``assets/``, by reading the handover slice's ``source_id``, or by matching a display
   name. That rediscovery is exactly the fail-open this slice exists to close.
3. **No path is ever reconstructed from a manifest key.** The package is re-walked with
   :func:`package_filesystem.walk_package` - the same no-follow walk S1 used - and only paths the
   WALK produced are opened. A key that the walk did not produce is a finding, not an ``open()``.

Cohort, not package, because a consumer cannot prove its provider alone
----------------------------------------------------------------------
A workbook whose ``migration-spec.json`` declares a Tableau PUBLISHED datasource has no model of its
own: its report binds to the model built from the provider's ``.tds``. Whether exactly one such
provider exists is a question about the SET of packages, so the verifier takes a sequence and
resolves the edge inside it. A consumer supplied alone is BLOCKED, because "I cannot see a provider"
and "there is no provider" are the same answer from one package and only the operator can widen the
invocation.

⚠️ **Stable identity only.** Provider matching is datasource LUID first, then the exact
``<site>/<name>`` published key when a LUID is genuinely unavailable on either side. A display name,
a folder stem, ``bound_datasource``, ``published_ds_name`` and the handover slice's ``source_id``
are diagnostics; none of them may admit a provider, an asset or an evidence record here.
"""

from __future__ import annotations

import json
import sys
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

import package_filesystem as pfs  # noqa: E402  # pylint: disable=wrong-import-position
from bundle_corpus import (  # noqa: E402  # pylint: disable=wrong-import-position
    PACKAGE_MARKER,
    TargetClassification,
    classify_target,
)

# ---------------------------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------------------------

RoleState = Literal["resolved", "not_applicable", "missing", "ambiguous", "mismatch"]
PackageKind = Literal["workbook", "datasource"]
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

#: The brief's frontmatter ``scope`` value implied by each topology. S2 checks IDENTITY only - unit
#: and scope - and leaves every policy field (fidelity bar, fallback authorization, refresh strategy,
#: grade) to the START_READY brief-policy parser, which is a separate prerequisite.
TOPOLOGY_SCOPE = {
    TOPOLOGY_OWNED_MODEL: "model_and_report",
    TOPOLOGY_STANDALONE_DATASOURCE: "model_only",
    TOPOLOGY_PUBLISHED_PROVIDER: "model_only",
    TOPOLOGY_PUBLISHED_CONSUMER: "report_only_shared_model",
}

#: Recorded when a source is genuinely local: no Tableau Server LUID exists to agree with, and the
#: SHA/filename/spec axes all do. It does NOT convert a role state - it says why one is N/A.
LIMITATION_LOCAL_SOURCE = "local_source_no_server_luid"
#: The packaged brief carries no ``+++`` frontmatter, so only its PRESENCE and bytes are established
#: here. Parsing free-form Markdown as policy is refused outright; the typed policy object is a
#: START_READY prerequisite, tracked as a limitation rather than silently inferred.
LIMITATION_BRIEF_POLICY_UNPARSED = "brief_policy_not_parsed"

# Stable diagnostic codes. ⚠️ These are printed into shared verdicts: no host path, no customer
# text, no exception message. A package-RELATIVE path may travel in `RoleResult.paths`, because the
# package already publishes those in its own manifest.
CODE_NOT_A_PACKAGE = "package_boundary_not_declared"
CODE_UNSAFE_TARGET = "package_boundary_unsafe"
CODE_INTEGRITY_NOT_CLEAN = "package_integrity_not_clean"
CODE_INTEGRITY_CHANGED = "package_changed_since_integrity_check"
CODE_MANIFEST_UNREADABLE = "package_manifest_unreadable"
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
CODE_BRIEF_FRONTMATTER = "brief_frontmatter_unparseable"
CODE_BRIEF_UNIT = "brief_unit_mismatch"
CODE_BRIEF_SCOPE = "brief_scope_mismatch"
CODE_PROVIDER_MISSING = "provider_missing"
CODE_PROVIDER_AMBIGUOUS = "provider_ambiguous"
CODE_PROVIDER_LUID_CONTRADICTION = "provider_luid_contradiction"
CODE_PROVIDER_BINDING = "provider_binding_mismatch"
CODE_PROVIDER_MODEL = "provider_model_unresolved"


# ---------------------------------------------------------------------------------------------
# typed result
# ---------------------------------------------------------------------------------------------


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
    """What this package's source bytes ARE, on every axis that is available.

    ``sha256`` is the S1-verified digest of the declared source role; ``tableau_luid`` is the LUID of
    this package's OWN kind (workbook LUID for a workbook, datasource LUID for a datasource) and is
    ``None`` for a genuinely local source; ``published_key`` is the exact ``<site>/<name>`` dedup key
    when the spec establishes one.
    """

    kind: PackageKind
    sha256: str | None
    tableau_luid: str | None
    published_key: str | None

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
    """An S1 clearance BOUND to the root it was computed for.

    ⚠️ The binding is the point. A caller may pass a clearance in to avoid re-hashing a large
    package, but :func:`verify_phase1_role_identity` matches it to a root by that root's own value -
    a clearance for a different path is ignored and the root is re-verified, so no caller can hand
    this verifier someone else's answer.
    """

    root: Path
    classification: TargetClassification
    integrity: pfs.PackageFilesystemResult


def verify_s1(root: Path) -> VerifiedPackage:
    """Classify ``root`` and verify its manifest against its bytes - the S1 prerequisite, bound."""
    classification = classify_target(root)
    integrity = pfs.verify_package(root, classification)
    return VerifiedPackage(root=root, classification=classification, integrity=integrity)


# ---------------------------------------------------------------------------------------------
# per-package facts
# ---------------------------------------------------------------------------------------------


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
    roles: list[RoleResult] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    dependencies: list[DependencyResult] = field(default_factory=list)
    topology: str | None = None
    source_key: str | None = None
    source_sha: str | None = None
    source_luid: str | None = None
    published_key: str | None = None
    declared_dependencies: tuple[tuple[str | None, str | None], ...] = ()
    model_role: str | None = None

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
        try:
            return json.loads(self.walked[key].read_text(encoding="utf-8"))
        except (OSError, ValueError):
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


def _declared_string(mapping: Any, key: str) -> str | None:
    """A declaration is a non-empty string or it is absent. ``null``, 0 and ``{}`` are all absent."""
    if not isinstance(mapping, dict):
        return None
    value = mapping.get(key)
    return value if isinstance(value, str) and value.strip() else None


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

    # ⚠️ The walk is re-run rather than reconstructed. S1 proved declared == walked, so this both
    # gives us walk-produced paths to open and notices a package that changed underneath us.
    walked, walk_rows, _empty = pfs.walk_package(root)
    walked.pop(PACKAGE_MARKER, None)
    manifest_text = (root / PACKAGE_MARKER).read_text(encoding="utf-8", errors="replace")
    try:
        manifest = pfs.parse_manifest_text(manifest_text)
        declared = pfs.declared_files(manifest)
    except Exception:  # pylint: disable=broad-exception-caught  # every refusal is typed, never raised
        return _blocked(cleared.classification.unit_name or None, None, CODE_MANIFEST_UNREADABLE)
    digests = {key: value for key, value in declared.items() if isinstance(key, str) and isinstance(value, str)}
    if walk_rows or set(digests) != set(walked):
        return _blocked(cleared.classification.unit_name or None, None, CODE_INTEGRITY_CHANGED)

    unit = _declared_string(manifest, "unit")
    if unit is None:
        return _blocked(None, None, CODE_UNIT_MISSING)
    kind = _declared_string(manifest, "kind")
    if kind not in (KIND_WORKBOOK, KIND_DATASOURCE):
        return _blocked(unit, None, CODE_KIND_UNCLASSIFIED)
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}
    return _facts_with_source(
        _Facts(
            root=root,
            unit=unit,
            kind=kind,
            manifest=manifest,
            artifacts=artifacts,
            digests=digests,
            walked=walked,
        )
    )


def _facts_with_source(facts: _Facts) -> _Facts:
    """Fill in the source-role key, its verified digest, and the spec's published dependencies."""
    declared_asset = _declared_string(facts.artifacts, "asset")
    if declared_asset is not None and declared_asset in facts.digests:
        facts.source_key = declared_asset
        facts.source_sha = facts.digests[declared_asset]
    spec = facts.json(_declared_string(facts.artifacts, "migration_spec"))
    facts.declared_dependencies = _spec_dependencies(spec)
    if facts.kind == KIND_DATASOURCE:
        facts.published_key = next((key for _luid, key in facts.declared_dependencies if key), None)
    return facts


def _spec_dependencies(spec: Any) -> tuple[tuple[str | None, str | None], ...]:
    """`(datasource luid, published key)` for every PUBLISHED datasource the spec declares.

    ⚠️ Only a **stable** identity is carried out of here. ``luid`` is the optional server identity a
    producer stamps when it genuinely has one; ``key`` is the exact ``<site>/<name>`` dedup key
    `parse_tableau.py` builds. ``id``, ``path``, ``derived_from`` and the datasource caption are
    display names and never leave this function.
    """
    if not isinstance(spec, dict):
        return ()
    found: list[tuple[str | None, str | None]] = []
    for row in spec.get("data_sources") or []:
        published = row.get("published_datasource") if isinstance(row, dict) else None
        if not isinstance(published, dict):
            continue
        luid = _declared_string(published, "luid")
        key = _declared_string(published, "key")
        if luid is None and key is None:
            continue
        if (luid, key) not in found:
            found.append((luid, key))
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
        return _role(role, STATE_MISMATCH, cardinality, [declared], CODE_ROLE_NOT_VERIFIED)
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
    workbooks = _names(report.get("workbooks"))
    datasources = _names(report.get("datasources"))
    mine, theirs = (workbooks, datasources) if facts.kind == KIND_WORKBOOK else (datasources, workbooks)
    if mine.count(facts.unit) != 1 or facts.unit in theirs:
        return _role(ROLE_ENGINE_CLASSIFICATION, STATE_MISMATCH, "1 row", ["report.json"], CODE_ENGINE_MEMBERSHIP)
    return _role(ROLE_ENGINE_CLASSIFICATION, STATE_RESOLVED, "1 row", ["report.json"])


def _names(rows: Any) -> list[str]:
    return [row["name"] for row in rows or [] if isinstance(row, dict) and isinstance(row.get("name"), str)]


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


def _provenance_role(facts: _Facts) -> tuple[RoleResult, dict[str, Any] | None]:
    """Exactly one provenance row, and it must be about the source role's own bytes."""
    payload = facts.json("source-provenance.json")
    if not isinstance(payload, dict):
        return _role(ROLE_SOURCE_PROVENANCE, STATE_MISSING, "1 file", [], CODE_ROLE_MISSING), None
    if _scope_unit(payload) != facts.unit:
        return (
            _role(ROLE_SOURCE_PROVENANCE, STATE_MISMATCH, "1 file", ["source-provenance.json"], CODE_SCOPE_MISMATCH),
            None,
        )
    rows = [row for row in payload.get("inputs") or [] if isinstance(row, dict)]
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
    supplied = row.get("input") if isinstance(row.get("input"), dict) else {}
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
    source = spec.get("source") if isinstance(spec, dict) and isinstance(spec.get("source"), dict) else {}
    if _declared_string(source, "file_name") != basename:
        return _role(ROLE_SOURCE_IDENTITY, STATE_MISMATCH, "1 sha256", [facts.source_key], CODE_SPEC_FILE)
    return _role(ROLE_SOURCE_IDENTITY, STATE_RESOLVED, "1 sha256", [facts.source_key])


#: The two LUID namespaces, keyed by the kind that owns each. ⚠️ They are NEVER interchangeable: a
#: datasource LUID in a workbook's provenance is a category error, not a spelling difference, and
#: reading one as the other is how a name-free comparison silently compares nothing.
LUID_FIELD = {KIND_WORKBOOK: "workbook_luid", KIND_DATASOURCE: "datasource_luid"}


def _server_identity_role(facts: _Facts, row: dict[str, Any] | None) -> RoleResult:
    """The Tableau Server LUID, when one exists - and an EARNED ``not_applicable`` when none does."""
    if row is None or facts.source_key is None:
        return _role(ROLE_SERVER_IDENTITY, STATE_MISSING, "0..1 luid", [], CODE_SOURCE_SHA_MISSING)
    origin = row.get("origin") if isinstance(row.get("origin"), dict) else {}
    mine = LUID_FIELD[facts.kind]
    theirs = LUID_FIELD[KIND_DATASOURCE if facts.kind == KIND_WORKBOOK else KIND_WORKBOOK]
    if _declared_string(origin, theirs) is not None:
        return _role(ROLE_SERVER_IDENTITY, STATE_MISMATCH, "0..1 luid", [], CODE_LUID_NAMESPACE)
    recorded = _declared_string(origin, mine)
    stamped = _filename_luid(PurePosixPath(facts.source_key).name)
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


def _receipt_role(facts: _Facts, required: Mapping[str, str]) -> RoleResult:
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
    outputs = [
        row["path"]
        for row in payload.get("artifacts") or []
        if isinstance(row, dict) and isinstance(row.get("path"), str)
    ]
    if not outputs:
        return _role(ROLE_ENGINE_RECEIPT, STATE_MISSING, "1..N outputs", [], CODE_RECEIPT_UNCOVERED)
    foreign = sorted(path for path in outputs if path not in facts.digests)
    if foreign:
        return _role(ROLE_ENGINE_RECEIPT, STATE_MISMATCH, "1..N outputs", foreign, CODE_RECEIPT_FOREIGN_OUTPUT)
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


def _brief_role(facts: _Facts, topology: str) -> tuple[RoleResult, list[str]]:  # pylint: disable=too-many-return-statements
    """The packaged, immutable brief: present as a package role, and identifying THIS unit.

    Two things are established here and no more. **Presence**: the brief is one declared, hashed file
    inside the package, so a stateless agent handed only the package has it - and the manifest
    records no external path, because an absolute path to the dispatcher's copy is a host disclosure
    that stops being true the moment anything moves. **Identity**: when the file opens with strict
    ``+++`` TOML frontmatter, its ``unit`` and ``scope`` must agree with this package.

    ⚠️ Everything else in the brief is left alone. Reading policy out of free-form Markdown would
    make wording into a gate; the typed policy object is a START_READY prerequisite, and its absence
    is recorded as :data:`LIMITATION_BRIEF_POLICY_UNPARSED` rather than guessed at.
    """
    declared = _declared_string(facts.artifacts, "migration_brief")
    candidates = [BRIEF_NAME] if BRIEF_NAME in facts.digests else []
    result = _declared_role(ROLE_MIGRATION_BRIEF, "1 file", declared, candidates, facts.digests)
    if result.blocks:
        return result, []
    text = facts.text(declared) or ""
    if not text.startswith("+++"):
        return result, [LIMITATION_BRIEF_POLICY_UNPARSED]
    _head, delimiter, rest = text[3:].partition("\n+++")
    if not delimiter:
        return _role(ROLE_MIGRATION_BRIEF, STATE_MISMATCH, "1 file", [BRIEF_NAME], CODE_BRIEF_FRONTMATTER), []
    try:
        front = tomllib.loads(_head)
    except (tomllib.TOMLDecodeError, ValueError):
        return _role(ROLE_MIGRATION_BRIEF, STATE_MISMATCH, "1 file", [BRIEF_NAME], CODE_BRIEF_FRONTMATTER), []
    del rest
    if front.get("unit") != facts.unit:
        return _role(ROLE_MIGRATION_BRIEF, STATE_MISMATCH, "1 file", [BRIEF_NAME], CODE_BRIEF_UNIT), []
    scope = front.get("scope")
    if scope is not None and scope != TOPOLOGY_SCOPE[topology]:
        return _role(ROLE_MIGRATION_BRIEF, STATE_MISMATCH, "1 file", [BRIEF_NAME], CODE_BRIEF_SCOPE), []
    return result, [] if scope is not None else [LIMITATION_BRIEF_POLICY_UNPARSED]


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
    if not keys:
        return _role(role, STATE_NOT_APPLICABLE, "0..1 dir", [])
    manifest_key = f"{directory}/{manifest_name}"
    if manifest_key not in facts.digests:
        return _role(role, STATE_MISSING, "1 manifest", keys, CODE_ROLE_MISSING)
    payload = facts.json(manifest_key)
    if not isinstance(payload, dict):
        return _role(role, STATE_MISMATCH, "1 manifest", [manifest_key], CODE_EVIDENCE_MANIFEST)
    records, named = (
        _reference_records(payload, directory)
        if role == ROLE_TABLEAU_REFERENCE
        else _oracle_records(payload, directory)
    )
    if not records:
        # A manifest declaring no record is an EMPTY capture, not evidence: it owns nothing, so it
        # cannot be a visual-evidence provider and it cannot be foreign either.
        return _role(role, STATE_NOT_APPLICABLE, "0 records", [manifest_key])
    if role == ROLE_TABLEAU_REFERENCE:
        owner = _declared_string(payload, "source_workbook_sha256")
        if owner is None or facts.source_sha is None or owner.casefold() != facts.source_sha.casefold():
            return _role(role, STATE_MISMATCH, "1 manifest", [manifest_key], CODE_EVIDENCE_FOREIGN)
    claims = {claim for record in records for claim in record if claim is not None}
    mine = {facts.source_luid} if facts.source_luid else set()
    # ⚠️ The two providers are held to different rules because they carry different identities. A
    # reference capture is owned by the source SHA, which is checked above, so a LUID it does not
    # state cannot contradict anything - only a STATED foreign one can. An oracle record has no SHA
    # at all: `workbook_luid` is the whole of its ownership claim, so a view that states none is
    # unattributed rather than harmlessly quiet.
    foreign = claims - mine if role == ROLE_TABLEAU_REFERENCE else claims ^ mine
    if foreign:
        return _role(role, STATE_MISMATCH, "1 owner", [manifest_key], CODE_EVIDENCE_FOREIGN)
    unaccounted = sorted(set(keys) - named - {manifest_key})
    if unaccounted:
        return _role(role, STATE_MISMATCH, "0..N files", unaccounted, CODE_EVIDENCE_UNACCOUNTED)
    return _role(role, STATE_RESOLVED, "1 dir", [manifest_key])


def _reference_records(payload: dict[str, Any], directory: str) -> tuple[list[tuple[str | None, ...]], set[str]]:
    """`(per-state LUID claims, named package keys)` for a `reference/manifest.json`.

    The manifest's own shape: ``dashboards[].states[].image`` names the render, and a workbook LUID
    may be claimed at manifest, entry or state scope. Every claim is collected, because a narrower
    scope is not an override - `reference_evidence._reference_workbook_luid` decided that already.
    """
    manifest_claim = _declared_string(payload, "workbook_luid") or _declared_string(payload, "source_workbook_luid")
    records: list[tuple[str | None, ...]] = []
    named: set[str] = set()
    for entry in payload.get("dashboards") or []:
        if not isinstance(entry, dict):
            continue
        entry_claim = _declared_string(entry, "workbook_luid") or _declared_string(entry, "source_workbook_luid")
        for state in entry.get("states") or []:
            if not isinstance(state, dict):
                continue
            state_claim = _declared_string(state, "workbook_luid") or _declared_string(state, "source_workbook_luid")
            records.append((manifest_claim, entry_claim, state_claim))
            image = _declared_string(state, "image")
            if image:
                named.add(f"{directory}/{image.lstrip('/')}")
    return records, named


def _oracle_records(payload: dict[str, Any], directory: str) -> tuple[list[tuple[str | None, ...]], set[str]]:
    """`(per-view LUID claims, named package keys)` for a packaged `oracle-manifest.json`."""
    records: list[tuple[str | None, ...]] = []
    named: set[str] = set()
    for view in payload.get("views") or []:
        if not isinstance(view, dict):
            continue
        records.append((_declared_string(view, "workbook_luid"),))
        for leg in ("image", "svg", "pdf", "data"):
            payload_leg = view.get(leg)
            if not isinstance(payload_leg, dict):
                continue
            for entry in (payload_leg.get("path"), payload_leg.get("retained_path")):
                if isinstance(entry, str) and entry.strip():
                    named.add(f"{directory}/{entry.lstrip('/')}")
    return records, named


# ---------------------------------------------------------------------------------------------
# topology and the cohort edge
# ---------------------------------------------------------------------------------------------


def _topology(facts: _Facts, consumed: Mapping[str, int]) -> str:
    """What SHAPE this package is - decided from the engine's kind plus the spec's own dependencies.

    A datasource package that also emits a self-service report is still a datasource: kind comes from
    the engine's classification, never from the filesystem, so an auxiliary `.Report` cannot promote
    it into a workbook.
    """
    if facts.kind == KIND_WORKBOOK:
        return TOPOLOGY_PUBLISHED_CONSUMER if facts.declared_dependencies else TOPOLOGY_OWNED_MODEL
    return TOPOLOGY_PUBLISHED_PROVIDER if consumed.get(facts.unit) else TOPOLOGY_STANDALONE_DATASOURCE


def _provider_matches(
    dependency: tuple[str | None, str | None], providers: Sequence[_Facts]
) -> tuple[list[_Facts], list[_Facts]]:
    """`(matched by datasource LUID, matched by exact published key)` across the cohort.

    Both axes are computed even though only one may be USED, because the disagreement between them
    is itself a finding: a package that answers the exact `<site>/<name>` key while carrying a
    different datasource LUID is not a near-miss provider, it is a contradiction.
    """
    luid, key = dependency
    by_luid = [p for p in providers if luid and p.source_luid and p.source_luid.casefold() == luid.casefold()]
    by_key = [p for p in providers if key and p.published_key and p.published_key.casefold() == key.casefold()]
    return by_luid, by_key


def _dependency(  # pylint: disable=too-many-return-statements
    facts: _Facts, dependency: tuple[str | None, str | None], providers: Sequence[_Facts]
) -> DependencyResult:
    """Resolve one published edge to exactly one provider package, and check the report binds to it."""
    luid, key = dependency
    by_luid, by_key = _provider_matches(dependency, providers)
    if luid is not None and not by_luid and by_key:
        named = by_key[0].unit if len(by_key) == 1 else None
        return DependencyResult(STATE_MISMATCH, luid, key, named, code=CODE_PROVIDER_LUID_CONTRADICTION)
    matches = by_luid if luid is not None else by_key
    if not matches:
        return DependencyResult(STATE_MISSING, luid, key, code=CODE_PROVIDER_MISSING)
    if len(matches) > 1:
        return DependencyResult(STATE_AMBIGUOUS, luid, key, code=CODE_PROVIDER_AMBIGUOUS)
    provider = matches[0]
    if provider.model_role is None:
        return DependencyResult(STATE_MISSING, luid, key, provider.unit, code=CODE_PROVIDER_MODEL)
    binding = facts.manifest.get("model_binding") if isinstance(facts.manifest.get("model_binding"), dict) else {}
    declared = _declared_string(binding, "path")
    target = PurePosixPath(declared).name if declared else None
    if binding.get("kind") != "byPath" or target != PurePosixPath(provider.model_role).name:
        return DependencyResult(STATE_MISMATCH, luid, key, provider.unit, provider.model_role, CODE_PROVIDER_BINDING)
    return DependencyResult(STATE_RESOLVED, luid, key, provider.unit, provider.model_role)


# ---------------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------------


def verify_phase1_role_identity(
    package_roots: Sequence[Path], *, verified: Sequence[VerifiedPackage] = ()
) -> tuple[Phase1RoleIdentityResult, ...]:
    """One verdict per supplied root, resolved as a COHORT.

    ``verified`` may carry S1 clearances a caller has already computed; each is used only for the
    root it was BOUND to (see :class:`VerifiedPackage`), and any root without a matching clearance is
    verified here. A caller therefore cannot substitute one package's clearance for another's.

    ⚠️ Returns results; raises nothing for a bad package. Every refusal - unreadable manifest,
    missing role, contradictory LUID, absent provider - is a typed ``BLOCKED`` verdict, because a
    traceback out of a gate carries the host paths these verdicts exist to keep out of issues.
    """
    cleared = {str(entry.root): entry for entry in verified}
    facts: list[_Facts] = []
    results: dict[int, Phase1RoleIdentityResult] = {}
    for index, root in enumerate(package_roots):
        clearance = cleared.get(str(root)) or verify_s1(root)
        outcome = _facts(root, clearance)
        if isinstance(outcome, Phase1RoleIdentityResult):
            results[index] = outcome
            continue
        outcome.model_role = next(iter(_fabric_directories(outcome, ".SemanticModel")), None)  # pylint: disable=attribute-defined-outside-init
        facts.append(outcome)
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
    """Assign every package its topology and (for a consumer) its resolved provider edges."""
    providers = [entry for entry in facts if entry.kind == KIND_DATASOURCE]
    consumers = [entry for entry in facts if entry.kind == KIND_WORKBOOK and entry.declared_dependencies]
    for entry in facts:
        _identify(entry)
    consumed: dict[str, int] = {}
    edges: dict[str, list[DependencyResult]] = {}
    for consumer in consumers:
        rows = [_dependency(consumer, dependency, providers) for dependency in consumer.declared_dependencies]
        edges[str(consumer.root)] = rows
        for row in rows:
            if row.provider_unit:
                consumed[row.provider_unit] = consumed.get(row.provider_unit, 0) + 1
    for entry in facts:
        entry.topology = _topology(entry, consumed)
        entry.dependencies = edges.get(str(entry.root), [])


def _identify(facts: _Facts) -> None:
    """Establish the source SHA/LUID/published key BEFORE any cohort edge is resolved.

    Order matters: a provider's LUID is what a consumer's dependency matches on, so it has to exist
    before matching, and it may only exist if this package's own provenance/asset/filename agree.
    """
    _role_result, row = _provenance_role(facts)
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


def _verdict(facts: _Facts) -> Phase1RoleIdentityResult:
    """Fold one package's roles, identity and dependencies into its verdict."""
    topology = facts.topology or _topology(facts, {})
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
    return Phase1RoleIdentityResult(
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
        ),
        dependencies=tuple(facts.dependencies),
        blockers=tuple(dict.fromkeys(blockers)),
        authorized_limitations=tuple(dict.fromkeys(limitations)),
    )


def _evidence_and_handover_roles(facts: _Facts, topology: str) -> list[RoleResult]:
    """Handover and the Tableau evidence roles - all four EARNED N/A for a datasource package."""
    if topology in (TOPOLOGY_STANDALONE_DATASOURCE, TOPOLOGY_PUBLISHED_PROVIDER):
        return [
            _role(ROLE_HANDOVER, STATE_NOT_APPLICABLE, "0 file", []),
            _role(ROLE_TABLEAU_REFERENCE, STATE_NOT_APPLICABLE, "0 dir", []),
            _role(ROLE_TABLEAU_ORACLE, STATE_NOT_APPLICABLE, "0 dir", []),
            _role(ROLE_VISUAL_EVIDENCE, STATE_NOT_APPLICABLE, "0 provider", []),
        ]
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
    if not present:
        return _role(ROLE_TABLEAU_ORACLE, STATE_NOT_APPLICABLE, "0..1 dir", [])
    return _evidence_role(facts, ROLE_TABLEAU_ORACLE, present[0], "oracle-manifest.json")
