"""Role and cross-artifact identity for a handover package cohort - issue #562, slice S2.

These are DIRECT tests of `scripts/package_role_identity.py`. Every fixture is built by hand rather
than by running the packager, for one reason: the invariant under test is *"a role is a declaration
that the bytes confirm"*, and the interesting cases are the ones a correct producer never emits -
a declaration removed while its file stays, a LUID replaced, a provider that answers the right key
with the wrong identity. `tests/test_package_unit.py` and `tests/test_package_unit_gates.py` cover
the other direction, that the producer actually emits packages these verify.

⚠️ **Every assertion names a ROLE and a STATE**, never "the result is not clean". A test that only
asserts `verdict == BLOCKED` passes for any defect at all, including one it did not cause, and would
have kept passing through each of the mutations at the bottom of this file.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import package_role_identity as pri  # noqa: E402  # pylint: disable=wrong-import-position

WB_UNIT = "Revenue"
WB_LUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DS_UNIT = "Shared_Sales"
DS_LUID = "11111111-2222-3333-4444-555555555555"
PUBLISHED_KEY = "sales-site/shared_sales"


# --------------------------------------------------------------------------------------------
# fixture builders - a producer-shaped package, and the knobs each negative control turns
# --------------------------------------------------------------------------------------------


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, (dict, list)):
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    else:
        path.write_text(str(payload), encoding="utf-8")


def seal(package: Path, **manifest: Any) -> Path:
    """Write the `package-manifest.json` LAST, describing exactly the bytes now in the package.

    This is what makes every fixture below S1-clean by construction: S2 runs on a verified package,
    so a fixture that was not sealed would be testing the previous slice instead of this one.
    """
    files = {
        path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package.rglob("*"))
        if path.is_file() and path.name != "package-manifest.json"
    }
    manifest["contents"] = {"files": files}
    _write(package / "package-manifest.json", manifest)
    return package


def brief_text(unit: str, scope: str) -> str:
    """An explicit strict policy, not legacy identity-only frontmatter."""
    return (
        f'+++\nschema = "phase1-start-ready/v1"\nunit = "{unit}"\nscope = "{scope}"\n'
        'fallback_authorization = "stop"\n+++\n\nMigrate it.\n'
    )


def fabric_tree(
    package: Path, unit: str, *, report: bool = True, model: bool = True, binding: str | None = None
) -> list[str]:
    """The engine working copy, and the package-relative paths a receipt must then account for."""
    outputs: list[str] = []
    if report:
        pbir = {"version": "4.0", "datasetReference": {"byPath": {"path": binding or f"../{unit}.SemanticModel"}}}
        _write(package / "fabric" / f"{unit}.Report" / "definition.pbir", pbir)
        _write(package / "fabric" / f"{unit}.Report" / "definition" / "pages" / "pages.json", {"pageOrder": []})
        outputs.append(f"fabric/{unit}.Report/definition.pbir")
    if model:
        _write(package / "fabric" / f"{unit}.SemanticModel" / "definition" / "model.tmdl", "model Model\n")
        outputs.append(f"fabric/{unit}.SemanticModel/definition/model.tmdl")
    _write(package / "fabric" / f"{unit}.pbip", {"version": "1.0"})
    outputs.append(f"fabric/{unit}.pbip")
    return outputs


def workbook_package(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    root: Path,
    *,
    unit: str = WB_UNIT,
    luid: str | None = WB_LUID,
    published: dict[str, Any] | None = None,
    own_model: bool | None = None,
    binding: str | None = None,
    asset_name: str | None = None,
    body: str | None = None,
) -> Path:
    """A complete, truthful workbook package: every required role present and agreeing."""
    package = root
    package.mkdir(parents=True, exist_ok=True)
    name = asset_name or (f"{luid}_{unit}.twb" if luid else f"{unit}.twb")
    _write(package / "assets" / name, body or f"<workbook name='{unit}'/>\n")
    digest = hashlib.sha256((package / "assets" / name).read_bytes()).hexdigest()
    model = (published is None) if own_model is None else own_model

    data_sources: list[dict[str, Any]] = [{"id": "ds-1"}]
    if published is not None:
        data_sources = [
            {"id": "ds-1", "connection": {"class": "sqlproxy", "mode": "live"}, "published_datasource": published}
        ]
    _write(package / "migration-spec.json", {"source": {"file_name": name}, "data_sources": data_sources})
    _write(package / "migration-spec.schema.json", {"$schema": "http://json-schema.org/draft-07/schema#"})
    _write(
        package / "migration-brief.md",
        brief_text(unit, "report_only_shared_model" if published else "model_and_report"),
    )
    origin: dict[str, Any] = {"match": "sha256"}
    if luid:
        origin["workbook_luid"] = luid
    _write(
        package / "source-provenance.json",
        {"inputs": [{"input": {"file": name, "sha256": digest}, "origin": origin}], "scope": {"unit": unit}},
    )
    _write(package / "report.json", {"workbooks": [{"name": unit}], "datasources": [], "scope": {"unit": unit}})
    _write(package / "handover" / f"{unit}.json", {"workbook": {"name": unit}, "scope": {"unit": unit}})
    outputs = fabric_tree(package, unit, model=model, binding=binding)
    _write(
        package / "engine-output-receipt.json",
        {"engine": {"version": "2.339.0"}, "artifacts": [{"path": path} for path in outputs], "scope": {"unit": unit}},
    )
    _write(package / "oracle" / "dashboard" / "view.png", "png-bytes")
    _write(
        package / "oracle" / "oracle-manifest.json",
        {
            "views": [
                {
                    "view_luid": "99999999-0000-0000-0000-000000000000",
                    "view_name": "Overview",
                    "workbook_luid": luid,
                    "image": {"status": "ok", "path": "dashboard/view.png"},
                }
            ]
        },
    )
    return seal(
        package,
        unit=unit,
        kind="workbook",
        artifacts={
            "migration_spec": "migration-spec.json",
            "migration_spec_schema": "migration-spec.schema.json",
            "migration_brief": "migration-brief.md",
            "asset": f"assets/{name}",
            "asset_route": "handover.workbook.source_id",
            "report": f"fabric/{unit}.Report",
            "model": f"fabric/{unit}.SemanticModel" if model else None,
            "handover": f"handover/{unit}.json",
        },
        model_binding={"kind": "byPath", "path": binding or f"../{unit}.SemanticModel", "resolves_in_package": model},
    )


def datasource_package(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    root: Path,
    *,
    unit: str = DS_UNIT,
    luid: str | None = DS_LUID,
    published_key: str | None = None,
    asset_name: str | None = None,
) -> Path:
    """A complete, truthful datasource package - the shape the current producer could not emit."""
    package = root
    package.mkdir(parents=True, exist_ok=True)
    name = asset_name or (f"{luid}_{unit}.tdsx" if luid else f"{unit}.tds")
    _write(package / "assets" / name, f"<datasource name='{unit}'/>\n")
    digest = hashlib.sha256((package / "assets" / name).read_bytes()).hexdigest()
    published = {"id": unit, "site": "sales-site", "key": published_key} if published_key else None
    _write(
        package / "migration-spec.json",
        {
            "source": {"file_name": name},
            "data_sources": [{"id": "ds-1", **({"published_datasource": published} if published else {})}],
        },
    )
    _write(package / "migration-spec.schema.json", {"$schema": "http://json-schema.org/draft-07/schema#"})
    _write(package / "migration-brief.md", brief_text(unit, "model_only"))
    origin: dict[str, Any] = {"match": "packaged_bytes"}
    if luid:
        origin["datasource_luid"] = luid
    _write(
        package / "source-provenance.json",
        {"inputs": [{"input": {"file": name, "sha256": digest}, "origin": origin}], "scope": {"unit": unit}},
    )
    _write(package / "report.json", {"workbooks": [], "datasources": [{"name": unit}], "scope": {"unit": unit}})
    outputs = fabric_tree(package, unit, report=False)
    _write(
        package / "engine-output-receipt.json",
        {"engine": {"version": "2.339.0"}, "artifacts": [{"path": path} for path in outputs], "scope": {"unit": unit}},
    )
    return seal(
        package,
        unit=unit,
        kind="datasource",
        artifacts={
            "migration_spec": "migration-spec.json",
            "migration_spec_schema": "migration-spec.schema.json",
            "migration_brief": "migration-brief.md",
            "asset": f"assets/{name}",
            "asset_route": "input_manifest.staged_input_path",
            "report": None,
            "model": f"fabric/{unit}.SemanticModel",
            "handover": None,
        },
        model_binding={"kind": "no_report", "path": None, "resolves_in_package": True},
    )


def role(result: pri.Phase1RoleIdentityResult, name: str) -> pri.RoleResult:
    """The named role's row - the assertion surface. A missing row is itself a failure."""
    found = [row for row in result.roles if row.role == name]
    assert found, f"{name} was never assessed; roles were {[row.role for row in result.roles]}"
    return found[0]


def verify_one(package: Path) -> pri.Phase1RoleIdentityResult:
    return pri.verify_phase1_role_identity([package])[0]


# --------------------------------------------------------------------------------------------
# vocabulary pin - without it every comparison against a constant is vacuous
# --------------------------------------------------------------------------------------------


def test_the_state_and_verdict_vocabulary_is_pinned_to_its_literal_values() -> None:
    """Redefining a constant would otherwise change both sides of every assertion below."""
    assert (pri.STATE_RESOLVED, pri.STATE_NOT_APPLICABLE) == ("resolved", "not_applicable")
    assert (pri.STATE_MISSING, pri.STATE_AMBIGUOUS, pri.STATE_MISMATCH) == ("missing", "ambiguous", "mismatch")
    assert (pri.VERDICT_START_READY, pri.VERDICT_BLOCKED) == ("START_READY", "BLOCKED")
    assert pri.BLOCKING_STATES == frozenset({"missing", "ambiguous", "mismatch"})


# --------------------------------------------------------------------------------------------
# positives - the three shapes the audit's role matrix enumerates
# --------------------------------------------------------------------------------------------


def test_a_truthful_workbook_package_resolves_every_required_role(tmp_path: Path) -> None:
    result = verify_one(workbook_package(tmp_path / "Revenue"))

    assert result.verdict == pri.VERDICT_START_READY, result.blockers
    assert result.topology == pri.TOPOLOGY_OWNED_MODEL
    assert {row.role for row in result.roles if row.state in pri.BLOCKING_STATES} == set()
    assert role(result, pri.ROLE_SOURCE_ASSET).state == pri.STATE_RESOLVED
    assert role(result, pri.ROLE_FABRIC_MODEL).state == pri.STATE_RESOLVED
    assert role(result, pri.ROLE_VISUAL_EVIDENCE).state == pri.STATE_RESOLVED
    assert result.source_identity is not None and result.source_identity.tableau_luid == WB_LUID


def test_a_complete_standalone_datasource_resolves_and_earns_its_not_applicables(tmp_path: Path) -> None:
    """The fixture the previous audit could NOT call an S2 positive, now produced completely.

    The old datasource package carried `artifacts.asset: null`, no spec and an empty provenance input
    list, and the entry gate reported `NOT_APPLICABLE` at exit 0 - a correct reference verdict about
    a package with nothing to build from. Reference is still N/A here; asset, spec, provenance and
    model are not, and they are what makes this a positive.
    """
    result = verify_one(datasource_package(tmp_path / "Shared_Sales"))

    assert result.verdict == pri.VERDICT_START_READY, result.blockers
    assert result.topology == pri.TOPOLOGY_STANDALONE_DATASOURCE
    assert role(result, pri.ROLE_SOURCE_ASSET).state == pri.STATE_RESOLVED
    assert role(result, pri.ROLE_MIGRATION_SPEC).state == pri.STATE_RESOLVED
    assert role(result, pri.ROLE_SOURCE_PROVENANCE).state == pri.STATE_RESOLVED
    assert role(result, pri.ROLE_FABRIC_MODEL).state == pri.STATE_RESOLVED
    assert [row.state for row in result.roles if row.role in (pri.ROLE_HANDOVER, pri.ROLE_TABLEAU_ORACLE)] == [
        pri.STATE_NOT_APPLICABLE,
        pri.STATE_NOT_APPLICABLE,
    ]


def test_a_shared_provider_and_its_consumer_resolve_as_one_cohort(tmp_path: Path) -> None:
    """The whole reason the verifier takes a SEQUENCE: the provider edge is a cohort property."""
    provider = datasource_package(tmp_path / "Shared_Sales", published_key=PUBLISHED_KEY)
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "sales-site", "key": PUBLISHED_KEY, "luid": DS_LUID},
        binding=f"../../../{DS_UNIT}/fabric/{DS_UNIT}.SemanticModel",
    )

    provider_result, consumer_result = pri.verify_phase1_role_identity([provider, consumer])

    assert (provider_result.verdict, consumer_result.verdict) == (pri.VERDICT_START_READY, pri.VERDICT_START_READY)
    assert provider_result.topology == pri.TOPOLOGY_PUBLISHED_PROVIDER
    assert consumer_result.topology == pri.TOPOLOGY_PUBLISHED_CONSUMER
    assert role(consumer_result, pri.ROLE_FABRIC_MODEL).state == pri.STATE_NOT_APPLICABLE
    assert consumer_result.dependencies[0].provider_unit == DS_UNIT
    assert consumer_result.dependencies[0].model_role == f"fabric/{DS_UNIT}.SemanticModel"


def test_a_local_source_with_no_server_luid_resolves_by_sha_and_earns_luid_not_applicable(tmp_path: Path) -> None:
    """Absence of a server identity is EARNED here - it is not the same as a missing one."""
    package = workbook_package(tmp_path / "Revenue", luid=None)
    (package / "oracle" / "oracle-manifest.json").unlink()
    (package / "oracle" / "dashboard" / "view.png").unlink()
    _write(package / "reference" / "shot.png", "png-bytes")
    digest = hashlib.sha256((package / "assets" / f"{WB_UNIT}.twb").read_bytes()).hexdigest()
    _write(
        package / "reference" / "manifest.json",
        {"source_workbook_sha256": digest, "dashboards": [{"name": "Overview", "states": [{"image": "shot.png"}]}]},
    )
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert result.verdict == pri.VERDICT_START_READY, result.blockers
    assert role(result, pri.ROLE_SERVER_IDENTITY).state == pri.STATE_NOT_APPLICABLE
    assert pri.LIMITATION_LOCAL_SOURCE in result.authorized_limitations
    assert role(result, pri.ROLE_SOURCE_IDENTITY).state == pri.STATE_RESOLVED
    assert role(result, pri.ROLE_TABLEAU_REFERENCE).state == pri.STATE_RESOLVED


def test_two_packages_sharing_a_display_name_resolve_independently(tmp_path: Path) -> None:
    """Nothing here joins on a name, so identical unit names with distinct identities cannot collide.

    ⚠️ Both packages carry the SAME `unit` on purpose. It is a package-local scope key derived from a
    Tableau display name, and two genuinely distinct workbooks can share one; a cohort-level
    "duplicate unit" refusal would be a name join wearing a collision check. What must differ, and
    does, is the identity: different LUID, different bytes.
    """
    other_luid = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"
    first = workbook_package(tmp_path / "a" / "Revenue")
    second = workbook_package(tmp_path / "b" / "Revenue", luid=other_luid, body="<workbook name='Revenue' rev='2'/>\n")

    results = pri.verify_phase1_role_identity([first, second])

    assert [row.verdict for row in results] == [pri.VERDICT_START_READY, pri.VERDICT_START_READY]
    assert [row.unit for row in results] == [WB_UNIT, WB_UNIT]
    assert [row.source_identity.tableau_luid for row in results if row.source_identity] == [WB_LUID, other_luid]
    shas = {row.source_identity.sha256 for row in results if row.source_identity}
    assert len(shas) == 2, "two distinct workbooks must not share one source identity"


# --------------------------------------------------------------------------------------------
# the frozen negatives - the three controls that used to read READY at exit 0
# --------------------------------------------------------------------------------------------


def test_removing_the_asset_declaration_is_missing_even_though_the_file_remains(tmp_path: Path) -> None:
    """The fail-open this slice exists to close: the role is a DECLARATION, not a discovery.

    ⚠️ The assertion is on the ROLE and the STATE. `verdict == BLOCKED` alone would also pass if the
    file had been deleted, if the manifest had been corrupted, or if some unrelated role had broken -
    none of which is the claim.
    """
    package = workbook_package(tmp_path / "Revenue")
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"]["asset"] = None
    seal(package, **manifest)
    assert (package / "assets" / f"{WB_LUID}_{WB_UNIT}.twb").is_file(), "the bytes must still be there"

    result = verify_one(package)

    assert role(result, pri.ROLE_SOURCE_ASSET).state == pri.STATE_MISSING
    assert role(result, pri.ROLE_SOURCE_ASSET).code == pri.CODE_ROLE_UNDECLARED
    assert result.verdict == pri.VERDICT_BLOCKED
    assert result.source_identity is not None and result.source_identity.sha256 is None


def test_a_contradictory_workbook_luid_is_a_source_identity_mismatch(tmp_path: Path) -> None:
    """Two identities that disagree are LESS evidence than none - the oracle cannot rescue it."""
    package = workbook_package(tmp_path / "Revenue")
    provenance = json.loads((package / "source-provenance.json").read_text(encoding="utf-8"))
    provenance["inputs"][0]["origin"]["workbook_luid"] = "cccccccc-dddd-eeee-ffff-000000000000"
    _write(package / "source-provenance.json", provenance)
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_SERVER_IDENTITY).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_SERVER_IDENTITY).code == pri.CODE_LUID_CONTRADICTION
    assert result.verdict == pri.VERDICT_BLOCKED


def test_a_declared_oracle_file_no_record_accounts_for_is_foreign_evidence(tmp_path: Path) -> None:
    """S1 refuses an UNdeclared extra file; this is the declared-but-unattributed one it cannot see."""
    package = workbook_package(tmp_path / "Revenue")
    _write(package / "oracle" / "dashboard" / "stray.png", "png-bytes")
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_TABLEAU_ORACLE).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_TABLEAU_ORACLE).code == pri.CODE_EVIDENCE_UNACCOUNTED
    assert "oracle/dashboard/stray.png" in role(result, pri.ROLE_TABLEAU_ORACLE).paths
    assert result.verdict == pri.VERDICT_BLOCKED


def test_an_oracle_view_belonging_to_another_workbook_is_foreign_evidence(tmp_path: Path) -> None:
    """The other half of foreign evidence: the file is accounted for, the OWNER is someone else."""
    package = workbook_package(tmp_path / "Revenue")
    manifest = json.loads((package / "oracle" / "oracle-manifest.json").read_text(encoding="utf-8"))
    manifest["views"][0]["workbook_luid"] = "cccccccc-dddd-eeee-ffff-000000000000"
    _write(package / "oracle" / "oracle-manifest.json", manifest)
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_TABLEAU_ORACLE).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_TABLEAU_ORACLE).code == pri.CODE_EVIDENCE_FOREIGN


# --------------------------------------------------------------------------------------------
# scope, source and engine-output disagreements
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("artifact", "expected_role", "expected_code"),
    [
        ("source-provenance.json", pri.ROLE_SOURCE_PROVENANCE, pri.CODE_SCOPE_MISMATCH),
        ("report.json", pri.ROLE_ENGINE_CLASSIFICATION, pri.CODE_SCOPE_MISMATCH),
        ("engine-output-receipt.json", pri.ROLE_ENGINE_RECEIPT, pri.CODE_RECEIPT_SCOPE),
    ],
)
def test_an_artifact_scoped_to_another_unit_blocks(
    tmp_path: Path, artifact: str, expected_role: str, expected_code: str
) -> None:
    """A package composed from two units' artifacts describes neither of them."""
    package = workbook_package(tmp_path / "Revenue")
    payload = json.loads((package / artifact).read_text(encoding="utf-8"))
    payload["scope"] = {"unit": "SomeOtherUnit"}
    _write(package / artifact, payload)
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, expected_role).state == pri.STATE_MISMATCH
    assert role(result, expected_role).code == expected_code


def test_a_handover_slice_scoped_to_another_unit_blocks(tmp_path: Path) -> None:
    """The filename alone used to be the only claim that a slice belonged to this unit."""
    package = workbook_package(tmp_path / "Revenue")
    _write(package / "handover" / f"{WB_UNIT}.json", {"workbook": {"name": WB_UNIT}, "scope": {"unit": "Elsewhere"}})
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_HANDOVER).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_HANDOVER).code == pri.CODE_SCOPE_MISMATCH


def test_a_spec_naming_a_different_source_file_is_a_source_identity_mismatch(tmp_path: Path) -> None:
    package = workbook_package(tmp_path / "Revenue")
    _write(package / "migration-spec.json", {"source": {"file_name": "SomethingElse.twb"}, "data_sources": []})
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_SOURCE_IDENTITY).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_SOURCE_IDENTITY).code == pri.CODE_SPEC_FILE


def test_a_provenance_row_for_other_bytes_is_a_provenance_mismatch(tmp_path: Path) -> None:
    package = workbook_package(tmp_path / "Revenue")
    provenance = json.loads((package / "source-provenance.json").read_text(encoding="utf-8"))
    provenance["inputs"][0]["input"]["sha256"] = "0" * 64
    _write(package / "source-provenance.json", provenance)
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_SOURCE_PROVENANCE).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_SOURCE_PROVENANCE).code == pri.CODE_PROVENANCE_SHA


def test_a_datasource_luid_in_a_workbook_provenance_row_is_a_namespace_mismatch(tmp_path: Path) -> None:
    """The two UUID namespaces are typed. Reading one as the other fails OPEN if they ever collide."""
    package = workbook_package(tmp_path / "Revenue")
    provenance = json.loads((package / "source-provenance.json").read_text(encoding="utf-8"))
    provenance["inputs"][0]["origin"]["datasource_luid"] = DS_LUID
    _write(package / "source-provenance.json", provenance)
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_SERVER_IDENTITY).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_SERVER_IDENTITY).code == pri.CODE_LUID_NAMESPACE


def test_a_receipt_output_belonging_to_another_unit_blocks(tmp_path: Path) -> None:
    """A receipt that attests to files this package does not contain describes another build."""
    package = workbook_package(tmp_path / "Revenue")
    receipt = json.loads((package / "engine-output-receipt.json").read_text(encoding="utf-8"))
    receipt["artifacts"].append({"path": "fabric/SomeOtherUnit.Report/definition.pbir"})
    _write(package / "engine-output-receipt.json", receipt)
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_ENGINE_RECEIPT).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_ENGINE_RECEIPT).code == pri.CODE_RECEIPT_FOREIGN_OUTPUT


def test_a_receipt_that_does_not_account_for_the_model_role_blocks(tmp_path: Path) -> None:
    """ "1..N outputs" is a coverage claim about the fabric roles, not a row count."""
    package = workbook_package(tmp_path / "Revenue")
    receipt = json.loads((package / "engine-output-receipt.json").read_text(encoding="utf-8"))
    receipt["artifacts"] = [row for row in receipt["artifacts"] if ".SemanticModel" not in row["path"]]
    _write(package / "engine-output-receipt.json", receipt)
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_ENGINE_RECEIPT).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_ENGINE_RECEIPT).code == pri.CODE_RECEIPT_UNCOVERED


def test_a_unit_the_engine_classifies_as_neither_kind_blocks(tmp_path: Path) -> None:
    package = workbook_package(tmp_path / "Revenue")
    _write(package / "report.json", {"workbooks": [], "datasources": [], "scope": {"unit": WB_UNIT}})
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_ENGINE_CLASSIFICATION).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_ENGINE_CLASSIFICATION).code == pri.CODE_ENGINE_MEMBERSHIP


def test_a_datasource_asset_in_a_workbook_package_is_not_an_admissible_source_role(tmp_path: Path) -> None:
    """Extension is part of the role, so a `.tds` cannot fill a workbook's source role."""
    package = workbook_package(tmp_path / "Revenue", asset_name="Revenue.tds")

    result = verify_one(package)

    assert role(result, pri.ROLE_SOURCE_ASSET).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_SOURCE_ASSET).code == pri.CODE_ROLE_WRONG_CANDIDATE


# --------------------------------------------------------------------------------------------
# the brief
# --------------------------------------------------------------------------------------------


def test_a_package_with_no_brief_blocks(tmp_path: Path) -> None:
    """A stateless agent handed only the package would have nothing saying what this is FOR."""
    package = workbook_package(tmp_path / "Revenue")
    (package / "migration-brief.md").unlink()
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"]["migration_brief"] = None
    seal(package, **manifest)

    result = verify_one(package)

    assert role(result, pri.ROLE_MIGRATION_BRIEF).state == pri.STATE_MISSING
    assert result.verdict == pri.VERDICT_BLOCKED


def test_a_brief_declared_as_an_external_path_is_not_a_packaged_role(tmp_path: Path) -> None:
    """A path is not availability: the bytes have to be IN the package, under the role name."""
    package = workbook_package(tmp_path / "Revenue")
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    manifest["artifacts"]["migration_brief"] = "C:/dispatcher/migrations/workbooks/revenue/migration-brief.md"
    seal(package, **manifest)

    result = verify_one(package)

    assert role(result, pri.ROLE_MIGRATION_BRIEF).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_MIGRATION_BRIEF).code == pri.CODE_ROLE_NOT_VERIFIED


def test_a_brief_whose_frontmatter_names_another_unit_blocks(tmp_path: Path) -> None:
    package = workbook_package(tmp_path / "Revenue")
    _write(package / "migration-brief.md", brief_text("SomeOtherUnit", "model_and_report"))
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_MIGRATION_BRIEF).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_MIGRATION_BRIEF).code == pri.CODE_BRIEF_UNIT


def test_a_brief_whose_scope_contradicts_the_topology_blocks(tmp_path: Path) -> None:
    """`scope` is an identity claim about the unit's shape, so S2 checks it; policy is not read."""
    package = workbook_package(tmp_path / "Revenue")
    _write(package / "migration-brief.md", brief_text(WB_UNIT, "report_only_shared_model"))
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_MIGRATION_BRIEF).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_MIGRATION_BRIEF).code == pri.CODE_BRIEF_SCOPE


def test_a_brief_with_unparseable_frontmatter_blocks_rather_than_falling_back_to_prose(tmp_path: Path) -> None:
    package = workbook_package(tmp_path / "Revenue")
    _write(package / "migration-brief.md", "+++\nunit = \nthis is not toml\n+++\n")
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_MIGRATION_BRIEF).state == pri.STATE_MISMATCH
    assert role(result, pri.ROLE_MIGRATION_BRIEF).code == pri.CODE_BRIEF_FRONTMATTER


def test_a_free_form_brief_resolves_as_PRESENT_and_records_the_unparsed_policy_limitation(tmp_path: Path) -> None:
    """The explicit S2 boundary: free-form Markdown is never parsed as policy, and says so."""
    package = workbook_package(tmp_path / "Revenue")
    _write(package / "migration-brief.md", "# Migration brief\n\nFaithful re-creation, stop on a wall.\n")
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))

    result = verify_one(package)

    assert role(result, pri.ROLE_MIGRATION_BRIEF).state == pri.STATE_RESOLVED
    assert pri.LIMITATION_BRIEF_POLICY_UNPARSED in result.authorized_limitations
    assert result.verdict == pri.VERDICT_START_READY, result.blockers


# --------------------------------------------------------------------------------------------
# the published cohort
# --------------------------------------------------------------------------------------------


def test_a_consumer_with_no_provider_in_the_cohort_blocks(tmp_path: Path) -> None:
    """ "I cannot see a provider" and "there is no provider" are the same answer from one package."""
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "sales-site", "key": PUBLISHED_KEY, "luid": DS_LUID},
        binding=f"../{DS_UNIT}.SemanticModel",
    )

    result = verify_one(consumer)

    assert role(result, pri.ROLE_PUBLISHED_DEPENDENCY).state == pri.STATE_MISSING
    assert result.dependencies[0].code == pri.CODE_PROVIDER_MISSING
    assert result.verdict == pri.VERDICT_BLOCKED


def test_two_providers_answering_one_datasource_luid_are_ambiguous(tmp_path: Path) -> None:
    first = datasource_package(tmp_path / "a" / "Shared_Sales", published_key=PUBLISHED_KEY)
    second = datasource_package(tmp_path / "b" / "Shared_Sales_2", unit="Shared_Sales_2", published_key=PUBLISHED_KEY)
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "sales-site", "key": PUBLISHED_KEY, "luid": DS_LUID},
        binding=f"../{DS_UNIT}.SemanticModel",
    )

    results = pri.verify_phase1_role_identity([first, second, consumer])

    assert role(results[2], pri.ROLE_PUBLISHED_DEPENDENCY).state == pri.STATE_AMBIGUOUS
    assert results[2].dependencies[0].code == pri.CODE_PROVIDER_AMBIGUOUS


def test_a_provider_answering_the_key_with_a_different_luid_is_a_contradiction(tmp_path: Path) -> None:
    """LUID first. A provider that matches the weaker axis and contradicts the stronger is refused."""
    provider = datasource_package(
        tmp_path / "Shared_Sales", luid="99999999-9999-9999-9999-999999999999", published_key=PUBLISHED_KEY
    )
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "sales-site", "key": PUBLISHED_KEY, "luid": DS_LUID},
        binding=f"../{DS_UNIT}.SemanticModel",
    )

    results = pri.verify_phase1_role_identity([provider, consumer])

    assert results[1].dependencies[0].code == pri.CODE_PROVIDER_LUID_CONTRADICTION
    assert role(results[1], pri.ROLE_PUBLISHED_DEPENDENCY).state == pri.STATE_MISMATCH


def test_a_consumer_bound_to_its_own_model_instead_of_the_provider_blocks(tmp_path: Path) -> None:
    """A duplicate model is a mismatch, not a convenience - nothing can then say which numbers won."""
    provider = datasource_package(tmp_path / "Shared_Sales", published_key=PUBLISHED_KEY)
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "sales-site", "key": PUBLISHED_KEY, "luid": DS_LUID},
        own_model=True,
    )

    results = pri.verify_phase1_role_identity([provider, consumer])

    assert role(results[1], pri.ROLE_FABRIC_MODEL).state == pri.STATE_MISMATCH
    assert results[1].dependencies[0].code == pri.CODE_PROVIDER_BINDING


def test_a_provider_is_matched_by_the_exact_published_key_when_no_luid_is_available(tmp_path: Path) -> None:
    """The second axis, used only when the first is genuinely unavailable on both sides."""
    provider = datasource_package(tmp_path / "Shared_Sales", luid=None, published_key=PUBLISHED_KEY)
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "sales-site", "key": PUBLISHED_KEY},
        binding=f"../../../{DS_UNIT}/fabric/{DS_UNIT}.SemanticModel",
    )

    results = pri.verify_phase1_role_identity([provider, consumer])

    assert [row.verdict for row in results] == [pri.VERDICT_START_READY, pri.VERDICT_START_READY], results[1].blockers
    assert results[1].dependencies[0].published_key == PUBLISHED_KEY
    assert results[1].dependencies[0].datasource_luid is None


def test_a_provider_named_only_by_a_DISPLAY_name_never_resolves(tmp_path: Path) -> None:
    """The route this slice refuses to have: a caption is not an identity, at any cardinality."""
    provider = datasource_package(tmp_path / "Shared_Sales", luid=None)
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "sales-site", "key": None, "luid": None},
        binding=f"../{DS_UNIT}.SemanticModel",
    )

    results = pri.verify_phase1_role_identity([provider, consumer])

    assert results[1].topology == pri.TOPOLOGY_PUBLISHED_CONSUMER
    assert len(results[1].dependencies) == 1
    assert results[1].dependencies[0].code == "published_dependency_identity_missing"
    assert results[1].verdict == "BLOCKED"


def test_a_provider_whose_only_agreement_is_its_NAME_is_still_missing(tmp_path: Path) -> None:
    """The discriminating twin of the test above, one layer lower.

    Here the consumer DOES declare a stable identity - an exact published key - and the only package
    in the cohort answers a different one while sharing the datasource's display name and unit name.
    A name-shaped fallback anywhere in the matcher would resolve this; the honest answer is that no
    provider for that key was supplied. Without this, a mutation that added a name route survived
    the whole file, because the case above never reaches the matcher at all.
    """
    provider = datasource_package(tmp_path / "Shared_Sales", luid=None, published_key=PUBLISHED_KEY)
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "other-site", "key": "other-site/shared_sales", "luid": None},
        binding=f"../{DS_UNIT}.SemanticModel",
    )

    results = pri.verify_phase1_role_identity([provider, consumer])

    assert results[1].dependencies[0].code == pri.CODE_PROVIDER_MISSING
    assert role(results[1], pri.ROLE_PUBLISHED_DEPENDENCY).state == pri.STATE_MISSING
    assert results[1].verdict == pri.VERDICT_BLOCKED


# --------------------------------------------------------------------------------------------
# the S1 boundary this slice consumes rather than re-answers
# --------------------------------------------------------------------------------------------


def test_a_package_that_is_not_S1_clean_is_blocked_before_any_role_is_read(tmp_path: Path) -> None:
    package = workbook_package(tmp_path / "Revenue")
    (package / "assets" / "undeclared.txt").write_text("x\n", encoding="utf-8")

    result = verify_one(package)

    assert result.blockers == (pri.CODE_INTEGRITY_NOT_CLEAN,)
    assert result.roles == ()


def test_an_ordinary_directory_is_refused_rather_than_verified(tmp_path: Path) -> None:
    """ "There is nothing to check here" and "I checked" must not share an answer."""
    (tmp_path / "bundle").mkdir()

    result = verify_one(tmp_path / "bundle")

    assert result.blockers == (pri.CODE_NOT_A_PACKAGE,)
    assert result.verdict == pri.VERDICT_BLOCKED


def test_a_clearance_for_a_DIFFERENT_root_is_never_applied(tmp_path: Path) -> None:
    """The binding property: a caller cannot hand this verifier someone else's S1 answer."""
    good = workbook_package(tmp_path / "Revenue")
    damaged = workbook_package(tmp_path / "Other", unit="Other")
    (damaged / "assets" / "undeclared.txt").write_text("x\n", encoding="utf-8")
    clearance = pri.verify_s1(good)

    result = pri.verify_phase1_role_identity([damaged], verified=[clearance])[0]

    assert result.blockers == (pri.CODE_ROOT_BINDING_INVALID,), "a foreign clearance was accepted"


def test_a_bound_clearance_is_reverified_once_at_the_S2_seam(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An earlier clearance is an observation, not authority to read a later revision."""
    package = workbook_package(tmp_path / "Revenue")
    clearance = pri.verify_s1(package)
    calls = []
    original = pri.verify_s1

    def counted(root: Path) -> pri.VerifiedPackage:
        calls.append(root)
        return original(root)

    monkeypatch.setattr(pri, "verify_s1", counted)

    result = pri.verify_phase1_role_identity([package], verified=[clearance])[0]

    assert result.verdict == pri.VERDICT_START_READY, result.blockers
    assert calls == [package], "S2 must verify the current root and bytes exactly once"


def test_the_verifier_returns_no_source_path_anywhere_in_its_result(tmp_path: Path) -> None:
    """#558 is a separate slice: this one names a role, it never hands back a file to open."""
    result = verify_one(workbook_package(tmp_path / "Revenue"))

    payload = json.dumps(result.as_dict())
    assert str(tmp_path) not in payload
    assert not any(isinstance(value, Path) for row in result.roles for value in row.paths)
    assert all(not path.startswith(("/", "\\")) and ":" not in path for row in result.roles for path in row.paths)


def test_strict_brief_policy_is_frozen_and_nonserialized(tmp_path: Path) -> None:
    package = datasource_package(tmp_path / "Provider")
    result = verify_one(package)
    assert result.brief_policy == pri.BriefPolicy("model_only", "stop")
    assert "brief_policy" not in result.as_dict()
    with pytest.raises(AttributeError):
        result.brief_policy.requested_scope = "model_and_report"
    assert pri.brief_identity(brief_text(DS_UNIT, "model_only"), DS_UNIT, "model_only") == (None, False)


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize(
    "boundary",
    [
        "exact",
        "opening-suffix",
        "opening-prefix",
        "opening-space",
        "opening-tab",
        "opening-blank",
        "closing-suffix",
        "closing-prefix",
        "closing-space",
        "closing-tab",
        "closing-missing",
        "extra-exact",
        "extra-suffix",
        "extra-prefix",
        "extra-space",
    ],
)
def test_frontmatter_requires_two_exact_boundary_lines(tmp_path: Path, boundary: str, newline: str) -> None:
    """Malformed explicit policy is not prose, and no malformed boundary can authorize a fallback."""
    text = brief_text(DS_UNIT, "model_only").replace('"stop"', '"model_only_unvalidated"')
    lines = text.split("\n")
    opening = {
        "opening-suffix": "+++not-a-delimiter",
        "opening-prefix": "not-a-delimiter+++",
        "opening-space": " +++",
        "opening-tab": "+++\t",
        "opening-blank": "\n+++",
    }
    closing = {
        "closing-suffix": "+++not-a-delimiter",
        "closing-prefix": "not-a-delimiter+++",
        "closing-space": "+++ ",
        "closing-tab": "\t+++",
        "closing-missing": "",
    }
    extra = {
        "extra-exact": "+++",
        "extra-suffix": "+++not-a-delimiter",
        "extra-prefix": "not-a-delimiter+++",
        "extra-space": " +++ ",
    }
    lines[0] = opening.get(boundary, lines[0])
    lines[5] = closing.get(boundary, lines[5])
    if boundary in extra:
        lines.append(extra[boundary])
    text = "\n".join(lines).replace("\n", newline)
    expected_code = None if boundary == "exact" else "brief_frontmatter_unparseable"
    expected_policy = pri.BriefPolicy("model_only", "model_only_unvalidated") if boundary == "exact" else None
    assert pri.parse_brief_policy(text, DS_UNIT, "model_only") == (expected_code, expected_policy)

    package = datasource_package(tmp_path / "Provider")
    (package / "migration-brief.md").write_bytes(text.encode("utf-8"))
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    seal(package, **manifest)
    result = verify_one(package)
    assert role(result, "migration_brief").code == expected_code
    assert result.brief_policy == expected_policy
    assert result.is_start_ready is (boundary == "exact")


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ('fallback_authorization = "stop"', "fallback_authorization = 99", "brief_policy_invalid"),
        ('fallback_authorization = "stop"', 'fallback_authorization = ["stop"]', "brief_policy_invalid"),
        ('fallback_authorization = "stop"', 'fallback_authorization = "continue"', "brief_policy_invalid"),
        ('schema = "phase1-start-ready/v1"', 'schema = "phase1-start-ready/v2"', "brief_policy_invalid"),
        ('schema = "phase1-start-ready/v1"\n', "", "brief_policy_invalid"),
        ('scope = "model_only"\n', "", "brief_policy_invalid"),
        ('scope = "model_only"', 'scope = "model_and_report"', "brief_scope_mismatch"),
        (f'unit = "{DS_UNIT}"', 'unit = "Other"', "brief_unit_mismatch"),
        (
            'fallback_authorization = "stop"',
            'fallback_authorization = "stop"\nextra = "canary"',
            "brief_policy_invalid",
        ),
        (
            'fallback_authorization = "stop"',
            'fallback_authorization = "stop"\nfallback_authorization = "stop"',
            "brief_frontmatter_unparseable",
        ),
    ],
)
def test_strict_policy_refuses_exact_invalid_field(tmp_path: Path, old: str, new: str, code: str) -> None:
    package = datasource_package(tmp_path / "Provider")
    text = brief_text(DS_UNIT, "model_only").replace(old, new)
    _write(package / "migration-brief.md", text)
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    seal(package, **manifest)
    result = verify_one(package)
    assert role(result, "migration_brief").code == code
    assert result.brief_policy is None
    assert pri.brief_identity(text, DS_UNIT, "model_only") == (code, False)


@pytest.mark.parametrize("variant", ["plain", "identity-only", "absent"])
def test_missing_policy_never_infers_stop(tmp_path: Path, variant: str) -> None:
    package = datasource_package(tmp_path / "Provider")
    brief = package / "migration-brief.md"
    if variant == "absent":
        brief.unlink()
    else:
        text = "Migrate it; a model-only fallback is probably fine."
        if variant == "identity-only":
            text = brief_text(DS_UNIT, "model_only").replace('fallback_authorization = "stop"\n', "")
        _write(brief, text)
    manifest = json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))
    seal(package, **manifest)
    result = verify_one(package)
    assert result.brief_policy is None
    assert "brief_policy_not_parsed" in result.authorized_limitations
    assert result.is_start_ready is (variant != "absent")


@pytest.mark.parametrize("reverse", [False, True])
def test_selected_provider_ordinal_survives_duplicate_units_and_filtered_roots(tmp_path: Path, reverse: bool) -> None:
    first = datasource_package(tmp_path / "first", unit="Shared", luid=DS_LUID)
    second = datasource_package(tmp_path / "second", unit="Shared", luid=WB_LUID)
    consumer = workbook_package(
        tmp_path / "Consumer",
        published={"luid": DS_LUID},
        binding="../../../first/fabric/Shared.SemanticModel",
    )
    roots = [second, first] if reverse else [first, second]
    roots.insert(0, tmp_path / "absent")
    roots.append(consumer)
    result = pri.verify_phase1_role_identity(roots)[-1]
    assert result.is_start_ready, result.codes()
    selected = result.dependencies[0]
    assert selected.provider_ordinal == (2 if reverse else 1)
    assert roots[selected.provider_ordinal] == first
    assert selected.provider_unit == "Shared"
    assert "provider_ordinal" not in selected.as_dict()
