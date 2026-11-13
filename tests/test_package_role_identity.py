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

import copy
import gc
import hashlib
import inspect
import json
import os
import shutil
import sys
import types
import weakref
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

from test_package_filesystem import link_directory, link_file

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import package_role_identity as pri  # noqa: E402  # pylint: disable=wrong-import-position
import bundle_corpus  # noqa: E402  # pylint: disable=wrong-import-position

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


def numeric_brief_text(unit: str, scope: str, numeric_obligation: str) -> str:
    """Literal v2 commissioning text; existing fixtures deliberately remain v1."""
    return (
        f'+++\nschema = "phase1-start-ready/v2"\nunit = "{unit}"\nscope = "{scope}"\n'
        f'fallback_authorization = "stop"\nnumeric_obligation = "{numeric_obligation}"\n+++\n\nMigrate it.\n'
    )


def numeric_package(root: Path, numeric_obligation: str = "none", shape: str = "workbook") -> Path:
    """Independently seal a known brief and the existing package fixture's own identity."""
    if shape == "datasource":
        package = datasource_package(root)
        unit, scope = DS_UNIT, "model_only"
    else:
        package = workbook_package(root, published={"key": PUBLISHED_KEY} if shape == "consumer" else None)
        unit, scope = WB_UNIT, "report_only_shared_model" if shape == "consumer" else "model_and_report"
    (package / "migration-brief.md").write_bytes(numeric_brief_text(unit, scope, numeric_obligation).encode("utf-8"))
    return seal(package, **json.loads((package / "package-manifest.json").read_bytes()))


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


def published_authority(source_sha256: str, workbook_luid: str = WB_LUID) -> dict[str, Any]:
    """Literal acquired authority, deliberately independent of the consumer spec's optional LUID."""
    return {
        "schema": "tableau-published-dependencies/v1",
        "source_sha256": source_sha256,
        "workbook_luid": workbook_luid,
        "source_match": "sha256",
        "rows": [
            {
                "source_ordinal": 0,
                "published_key": PUBLISHED_KEY,
                "state": "resolved",
                "candidate_count": 1,
                "datasource_luid": DS_LUID,
            }
        ],
    }


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
        if published is not None:
            origin["published_dependencies"] = published_authority(digest, luid)
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


@pytest.mark.parametrize("reverse", [False, True], ids=["provider-first", "consumer-first"])
@pytest.mark.parametrize("source_match", ["sha256", "revision_same"])
def test_a_shared_provider_and_its_consumer_resolve_as_one_cohort(
    tmp_path: Path, reverse: bool, source_match: str
) -> None:
    """The whole reason the verifier takes a SEQUENCE: the provider edge is a cohort property."""
    provider = datasource_package(tmp_path / "Shared_Sales", published_key=PUBLISHED_KEY)
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "sales-site", "key": PUBLISHED_KEY},
        binding=f"../../../{DS_UNIT}/fabric/{DS_UNIT}.SemanticModel",
    )
    provenance = json.loads((consumer / "source-provenance.json").read_bytes())
    provenance["inputs"][0]["origin"]["published_dependencies"]["source_match"] = source_match
    if source_match == "revision_same":
        provenance["inputs"][0]["origin"]["match"] = "name_only"
    _write(consumer / "source-provenance.json", provenance)
    seal(consumer, **json.loads((consumer / "package-manifest.json").read_bytes()))
    assert (
        "luid"
        not in json.loads((consumer / "migration-spec.json").read_bytes())["data_sources"][0]["published_datasource"]
    )

    roots = [consumer, provider] if reverse else [provider, consumer]
    results = pri.verify_phase1_role_identity(roots)
    provider_result, consumer_result = results[roots.index(provider)], results[roots.index(consumer)]

    assert (provider_result.verdict, consumer_result.verdict) == (pri.VERDICT_START_READY, pri.VERDICT_START_READY)
    assert provider_result.topology == pri.TOPOLOGY_PUBLISHED_PROVIDER
    assert consumer_result.topology == pri.TOPOLOGY_PUBLISHED_CONSUMER
    assert role(consumer_result, pri.ROLE_FABRIC_MODEL).state == pri.STATE_NOT_APPLICABLE
    assert consumer_result.dependencies[0].provider_unit == DS_UNIT
    assert consumer_result.dependencies[0].model_role == f"fabric/{DS_UNIT}.SemanticModel"
    assert consumer_result.dependencies[0].datasource_luid == DS_LUID
    assert consumer_result.dependencies[0].provider_ordinal == roots.index(provider)
    assert "provider_ordinal" not in consumer_result.as_dict()["dependencies"][0]
    if source_match == "revision_same":
        assert consumer_result.source_identity.revision == pri.REVISION_UNCONFIRMED


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


# --------------------------------------------------------------------------------------------
# the brief
# --------------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------------
# the published cohort
# --------------------------------------------------------------------------------------------


def test_a_provider_answering_the_key_with_a_different_luid_is_a_contradiction(tmp_path: Path) -> None:
    """LUID first. A provider that matches the weaker axis and contradicts the stronger is refused."""
    provider = datasource_package(
        tmp_path / "Shared_Sales", luid="99999999-9999-9999-9999-999999999999", published_key=PUBLISHED_KEY
    )
    consumer = workbook_package(
        tmp_path / "Revenue",
        published={"id": DS_UNIT, "site": "sales-site", "key": PUBLISHED_KEY, "luid": DS_LUID},
        binding=f"../../../{DS_UNIT}/fabric/{DS_UNIT}.SemanticModel",
    )

    results = pri.verify_phase1_role_identity([provider, consumer])

    assert results[1].dependencies[0].code == pri.CODE_PROVIDER_LUID_CONTRADICTION
    assert role(results[1], pri.ROLE_PUBLISHED_DEPENDENCY).state == pri.STATE_MISMATCH


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


def test_the_verifier_returns_no_source_path_anywhere_in_its_result(tmp_path: Path) -> None:
    """#558 is a separate slice: this one names a role, it never hands back a file to open."""
    result = verify_one(workbook_package(tmp_path / "Revenue"))

    payload = json.dumps(result.as_dict())
    assert str(tmp_path) not in payload
    assert not any(isinstance(value, Path) for row in result.roles for value in row.paths)
    assert all(not path.startswith(("/", "\\")) and ":" not in path for row in result.roles for path in row.paths)


@pytest.mark.parametrize("numeric_obligation", ["required"], ids=["v2-required"])
def test_strict_brief_policy_is_frozen_and_nonserialized(tmp_path: Path, numeric_obligation: str | None) -> None:
    package = datasource_package(tmp_path / "Provider")
    if numeric_obligation is not None:
        (package / "migration-brief.md").write_bytes(
            numeric_brief_text(DS_UNIT, "model_only", numeric_obligation).encode("utf-8")
        )
        seal(package, **json.loads((package / "package-manifest.json").read_bytes()))
    result = verify_one(package)
    assert result.is_start_ready, result.blockers
    assert result.brief_policy == pri.BriefPolicy("model_only", "stop", numeric_obligation)
    assert result.brief_policy.numeric_obligation == numeric_obligation
    assert pri.BriefPolicy("model_only", "stop").numeric_obligation is None
    assert "brief_policy" not in result.as_dict()
    with pytest.raises(AttributeError):
        result.brief_policy.requested_scope = "model_and_report"
    with pytest.raises(AttributeError):
        result.brief_policy.numeric_obligation = "none"
    assert pri.brief_identity(brief_text(DS_UNIT, "model_only"), DS_UNIT, "model_only") == (None, False)


@pytest.mark.parametrize(("newline", "numeric_obligation"), [("\n", "none"), ("\r\n", None)], ids=["lf-v2", "crlf-v1"])
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
def test_frontmatter_requires_two_exact_boundary_lines(
    tmp_path: Path, boundary: str, newline: str, numeric_obligation: str | None
) -> None:
    """Malformed explicit policy is not prose, and no malformed boundary can authorize a fallback."""
    text = brief_text(DS_UNIT, "model_only").replace('"stop"', '"model_only_unvalidated"')
    if numeric_obligation is not None:
        text = numeric_brief_text(DS_UNIT, "model_only", numeric_obligation).replace(
            '"stop"', '"model_only_unvalidated"'
        )
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
    closing_index = 5 if numeric_obligation is None else 6
    lines[closing_index] = closing.get(boundary, lines[closing_index])
    if boundary in extra:
        lines.append(extra[boundary])
    text = "\n".join(lines).replace("\n", newline)
    expected_code = None if boundary == "exact" else "brief_frontmatter_unparseable"
    expected_policy = (
        pri.BriefPolicy("model_only", "model_only_unvalidated", numeric_obligation) if boundary == "exact" else None
    )
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
        ('fallback_authorization = "stop"', 'fallback_authorization = "continue"', "brief_policy_invalid"),
        ('schema = "phase1-start-ready/v1"', 'schema = "phase1-start-ready/v2"', "brief_policy_invalid"),
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


@pytest.mark.parametrize("variant", ["identity-only", "absent"])
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


@pytest.mark.parametrize("numeric_obligation", ["required"])
@pytest.mark.parametrize("newline", ["\n"], ids=["lf"])
@pytest.mark.parametrize(("shape", "unit", "scope"), [("consumer", WB_UNIT, "report_only_shared_model")])
def test_current_v2_brief_uses_package_identity_and_topology(
    tmp_path: Path, shape: str, unit: str, scope: str, numeric_obligation: str, newline: str
) -> None:
    package = numeric_package(tmp_path / "Folder_Is_Not_Identity", numeric_obligation, shape)
    raw = numeric_brief_text(unit, scope, numeric_obligation).replace("\n", newline).encode("utf-8")
    (package / "migration-brief.md").write_bytes(raw)
    seal(package, **json.loads((package / "package-manifest.json").read_bytes()))
    expected = pri.BriefPolicy(scope, "stop", numeric_obligation)
    assert pri.parse_brief_policy(raw.decode("utf-8"), unit, scope) == (None, expected)
    assert pri.read_current_brief_policy(package) == (None, expected)
    assert (package / "migration-brief.md").read_bytes() == raw


@pytest.mark.parametrize(
    ("old", "new", "code"),
    [
        ('numeric_obligation = "none"', 'numeric_obligation = "skip"', "brief_policy_invalid"),
        ('numeric_obligation = "none"', 'numeric_obligation = {value = "none"}', "brief_policy_invalid"),
        ('numeric_obligation = "none"', 'numeric_obligation = "none"\nextra = "none"', "brief_policy_invalid"),
        ('numeric_obligation = "none"', "numeric_obligation =", "brief_frontmatter_unparseable"),
        ('fallback_authorization = "stop"', 'fallback_authorization = "skip"', "brief_policy_invalid"),
        ('scope = "model_and_report"\n', "", "brief_policy_invalid"),
    ],
)
def test_v2_invalid_policy_never_falls_back_to_prose(tmp_path: Path, old: str, new: str, code: str) -> None:
    package = numeric_package(tmp_path / "Unit")
    text = numeric_brief_text(WB_UNIT, "model_and_report", "none").replace(old, new)
    text += '\nLegacy prose says numeric_obligation = "none"; this grants nothing.\n'
    assert pri.parse_brief_policy(text, WB_UNIT, "model_and_report") == (code, None)
    (package / "migration-brief.md").write_bytes(text.encode("utf-8"))
    seal(package, **json.loads((package / "package-manifest.json").read_bytes()))
    assert pri.read_current_brief_policy(package) == (code, None)
    result = verify_one(package)
    assert not result.is_start_ready and role(result, "migration_brief").code == code
    assert result.brief_policy is None


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("undeclared-role", "role_declaration_absent"),
        ("wrong-role-type", "identity_type_invalid"),
        ("foreign-role", "role_declaration_not_an_admissible_candidate"),
        ("aliased-role", "role_declaration_not_a_verified_file"),
        ("traversal-role", "role_declaration_not_a_verified_file"),
        ("missing-digest", "role_declaration_not_a_verified_file"),
        ("wrong-digest-type", "package_declared_digest_not_a_string"),
        ("malformed-digest", "package_declared_digest_malformed"),
        ("uppercase-digest", "package_declared_digest_malformed"),
        ("wrong-digest", "package_file_digest_mismatch"),
        ("aliased-digest", "package_declared_keys_collide"),
        ("changed-brief", "package_file_digest_mismatch"),
        ("invalid-utf8", "role_declaration_not_a_verified_file"),
    ],
)
def test_current_brief_requires_declared_role_digest_and_matching_held_bytes(
    tmp_path: Path, change: str, code: str
) -> None:
    package = numeric_package(tmp_path / "Unit")
    expected = (None, pri.BriefPolicy("model_and_report", "stop", "none"))
    assert pri.read_current_brief_policy(package) == expected
    manifest_path = package / "package-manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    files = manifest["contents"]["files"]
    artifacts = manifest["artifacts"]
    brief = package / "migration-brief.md"
    if change == "undeclared-role":
        del artifacts["migration_brief"]
    elif change == "wrong-role-type":
        artifacts["migration_brief"] = ["migration-brief.md"]
    elif change == "foreign-role":
        (package / "other.md").write_bytes(brief.read_bytes())
        files["other.md"] = files["migration-brief.md"]
        artifacts["migration_brief"] = "other.md"
    elif change == "aliased-role":
        artifacts["migration_brief"] = "Migration-Brief.md"
    elif change == "traversal-role":
        artifacts["migration_brief"] = "../migration-brief.md"
    elif change == "missing-digest":
        del files["migration-brief.md"]
    elif change == "wrong-digest-type":
        files["migration-brief.md"] = None
    elif change == "malformed-digest":
        files["migration-brief.md"] = "g" * 64
    elif change == "uppercase-digest":
        files["migration-brief.md"] = "A" * 64
    elif change == "wrong-digest":
        files["migration-brief.md"] = "0" * 64
    elif change == "aliased-digest":
        files["Migration-Brief.md"] = files["migration-brief.md"]
    elif change == "changed-brief":
        before = pri.revision.package_working_revision(package)
        brief.write_bytes(brief.read_bytes().replace(b'"none"', b'"required"'))
        assert pri.revision.package_working_revision(package) != before
    else:
        brief.write_bytes(b"\xff")
        files["migration-brief.md"] = hashlib.sha256(b"\xff").hexdigest()
    manifest_path.write_bytes(json.dumps(manifest).encode("utf-8"))
    assert pri.read_current_brief_policy(package) == (code, None)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("missing", "package_boundary_not_declared"),
        ("not-utf8", "package_manifest_unreadable"),
        ("malformed-json", "package_manifest_not_json"),
        ("not-object", "package_manifest_not_object"),
        ("duplicate-role", "package_manifest_duplicate_key"),
        ("duplicate-contents", "package_manifest_duplicate_key"),
        ("non-finite", "package_manifest_non_finite_number"),
        ("missing-contents", "package_contents_missing"),
        ("wrong-files-type", "package_contents_files_not_object"),
        ("wrong-artifacts-type", "identity_type_invalid"),
        ("unit", "brief_unit_mismatch"),
        ("missing-unit", "package_unit_missing"),
        ("unit-type", "identity_type_invalid"),
        ("unit-path", "identity_type_invalid"),
        ("kind", "package_kind_unclassified"),
        ("topology", "brief_scope_mismatch"),
        ("spec-role", "role_declaration_absent"),
    ],
)
def test_current_brief_rereads_manifest_authority(tmp_path: Path, change: str, code: str) -> None:
    package = numeric_package(tmp_path / "Unit")
    assert pri.read_current_brief_policy(package)[1].numeric_obligation == "none"
    target = package / "package-manifest.json"
    manifest = json.loads(target.read_bytes())
    if change == "missing":
        target.unlink()
    elif change == "not-utf8":
        target.write_bytes(b"\xff")
    elif change == "malformed-json":
        target.write_bytes(b'{"private":')
    elif change == "not-object":
        target.write_bytes(b"[]")
    elif change == "duplicate-role":
        text = json.dumps(manifest).replace('"migration_brief":', '"migration_brief": "other.md", "migration_brief":')
        target.write_bytes(text.encode())
    elif change == "duplicate-contents":
        text = json.dumps(manifest).replace('"contents":', '"contents": {}, "contents":')
        target.write_bytes(text.encode())
    elif change == "non-finite":
        text = json.dumps(manifest)[:-1] + ', "private": NaN}'
        target.write_bytes(text.encode())
    else:
        if change == "missing-contents":
            del manifest["contents"]
        elif change == "wrong-files-type":
            manifest["contents"]["files"] = []
        elif change == "wrong-artifacts-type":
            manifest["artifacts"] = []
        elif change == "unit":
            manifest["unit"] = "Other"
        elif change == "missing-unit":
            del manifest["unit"]
        elif change == "unit-type":
            manifest["unit"] = 1
        elif change == "unit-path":
            manifest["unit"] = "../Other"
        elif change == "kind":
            manifest["kind"] = "unknown"
        elif change == "topology":
            manifest["kind"] = "datasource"
        else:
            del manifest["artifacts"]["migration_spec"]
        target.write_bytes(json.dumps(manifest).encode())
    assert pri.read_current_brief_policy(package) == (code, None)


def test_current_brief_reads_held_bytes_once_without_whole_package_s1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = numeric_package(tmp_path / "Unit")
    expected = (package / "migration-brief.md").read_bytes()
    model = package / "fabric" / f"{WB_UNIT}.SemanticModel" / "definition" / "model.tmdl"
    report = package / "fabric" / f"{WB_UNIT}.Report" / "definition" / "pages" / "pages.json"
    model.write_bytes(b"model Edited\n")
    report.write_bytes(b'{"pageOrder":["edited"]}\n')
    assert "package_file_digest_mismatch" in pri.verify_s1(package).integrity.codes()
    read_bytes, parser = Path.read_bytes, pri.parse_brief_policy
    opened, parsed = [], []

    def tracked_read(path: Path) -> bytes:
        opened.append(path)
        assert path.name in ("package-manifest.json", "migration-brief.md", "migration-spec.json")
        return read_bytes(path)

    def tracked_parser(text: str, unit: str, scope: str) -> tuple:
        parsed.append((text, unit, scope))
        return parser(text, unit, scope)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("the current brief reader must not renew S1 or hash unrelated working files")

    monkeypatch.setattr(Path, "read_bytes", tracked_read)
    monkeypatch.setattr(pri, "parse_brief_policy", tracked_parser)
    monkeypatch.setattr(pri, "verify_s1", forbidden)
    monkeypatch.setattr(pri.pfs, "verify_package", forbidden)
    monkeypatch.setattr(pri.pfs, "_hash_file", forbidden)
    assert pri.read_current_brief_policy(package) == (None, pri.BriefPolicy("model_and_report", "stop", "none"))
    assert parsed == [(expected.decode("utf-8"), WB_UNIT, "model_and_report")]
    assert sorted(path.name for path in opened) == [
        "migration-brief.md",
        "migration-spec.json",
        "package-manifest.json",
    ]


@pytest.mark.parametrize("boundary", ["root", "ancestor", "manifest", "brief", "spec"])
def test_current_brief_refuses_links_before_opening_any_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    package = numeric_package(tmp_path / "outside" / "Unit")
    if boundary in ("root", "ancestor"):
        alias = tmp_path / "linked"
        link_directory(alias, package if boundary == "root" else package.parent)
        package = alias if boundary == "root" else alias / package.name
    else:
        name = {"manifest": "package-manifest.json", "brief": "migration-brief.md", "spec": "migration-spec.json"}[
            boundary
        ]
        original = package / name
        target = tmp_path / "outside-member"
        original.rename(target)
        link_file(original, target)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("an unsafe package boundary was opened before no-follow refusal")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert pri.read_current_brief_policy(package) == ("package_boundary_unsafe", None)


@pytest.mark.parametrize("reverse", [True])
def test_selected_provider_ordinal_survives_duplicate_units_and_filtered_roots(tmp_path: Path, reverse: bool) -> None:
    first = datasource_package(tmp_path / "first", unit="Shared", luid=DS_LUID, published_key=PUBLISHED_KEY)
    second = datasource_package(tmp_path / "second", unit="Shared", luid=WB_LUID)
    consumer = workbook_package(
        tmp_path / "Consumer",
        published={"key": PUBLISHED_KEY},
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


# W (#363): literal current snapshots, not packager/receipt output or original-history credentials.
# pylint: disable=protected-access,unidiomatic-typecheck
_W_ASSET = f"assets/{WB_LUID}_Revenue.twb"
_W_REPORT = "fabric/Revenue.Report"
_W_MODEL = "fabric/Revenue.SemanticModel"
_W_PBIP = "fabric/Revenue.pbip"
_W_CACHE = f"{_W_MODEL}/.pbi/cache.abf"
_W_LOCAL = {
    "schema": "phase1-data-access/v1",
    "state": "local_import_ready",
    "source_keys": [],
    "provider_unit": None,
    "provider_state": None,
    "validation": "validated",
    "effective_scope": "model_and_report",
    "max_phase2_claim": "data_validated",
    "codes": ["all-flat-file", "package-self-contained"],
}
_W_LIVE_CONNECTION = {
    "class": "sqlserver",
    "server": "source.example",
    "database": "db",
    "schema": "dbo",
    "name": "orders-leg",
    "powerbi_target": "live_source",
}


def _w_ordinary_key(server: str) -> str:
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


_W_LIVE_KEY = _w_ordinary_key("source.example")
_W_OTHER_KEY = _w_ordinary_key("other.example")


class _WorkingString(str):
    """Equal text is not the exact native revision/identity scalar."""


class _WorkingPath(type(Path())):
    """Equal paths cannot supply a native-Path boundary."""


def _w_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"


def _w_put(package: Path, name: str, raw: bytes) -> None:
    path = package.joinpath(*name.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def _w_package(root: Path, *, live: bool = False, projection: dict | None = None) -> tuple[Path, dict[str, bytes]]:
    """Independent names/bytes/digests; opaque model bytes intentionally prove no import eligibility."""
    source = b"<workbook name='Revenue'/>\n"
    raw = {
        _W_ASSET: source,
        "migration-spec.json": _w_json(
            {
                "source": {"file_name": _W_ASSET.split("/")[-1]},
                "data_sources": [
                    {
                        "id": "orders",
                        "tables": [{"name": "Orders", "table": "[dbo].[Orders]", "connection": "orders-leg"}]
                        if live
                        else [],
                        "fields": [],
                        "connection": _W_LIVE_CONNECTION
                        if live
                        else {"class": "excel-direct", "powerbi_target": "flat_file"},
                    }
                ],
            }
        ),
        "migration-spec.schema.json": b'{"type":"object","required":["source","data_sources"]}\n',
        "data-access.json": _w_json(projection if projection is not None else _W_LOCAL),
        "source-provenance.json": _w_json(
            {
                "scope": {"unit": "Revenue"},
                "inputs": [
                    {
                        "input": {"file": _W_ASSET.split("/")[-1], "sha256": hashlib.sha256(source).hexdigest()},
                        "origin": {"workbook_luid": WB_LUID},
                    }
                ],
            }
        ),
        "report.json": b'{"scope":{"unit":"Revenue"},"workbooks":[{"name":"Revenue"}],"datasources":[]}\n',
        "migration-brief.md": numeric_brief_text("Revenue", "model_and_report", "none").encode("utf-8"),
        _W_PBIP: b'{"version":"1.0","artifacts":[{"report":{"path":"Revenue.Report"}}]}\n',
        f"{_W_REPORT}/definition.pbir": b'{"version":"4.0","datasetReference":{"byPath":{"path":"../Revenue.SemanticModel"}}}\n',
        f"{_W_REPORT}/definition/pages/pages.json": b'{"pageOrder":[]}\n',
        f"{_W_MODEL}/definition/model.tmdl": b"model Model\n",
        "data/orders.csv": b"id,amount\n1,7\n",
    }
    manifest = {
        "unit": "Revenue",
        "kind": "workbook",
        "artifacts": {
            "asset": _W_ASSET,
            "migration_spec": "migration-spec.json",
            "migration_spec_schema": "migration-spec.schema.json",
            "data_access": "data-access.json",
            "migration_brief": "migration-brief.md",
            "report": _W_REPORT,
            "model": _W_MODEL,
        },
        "model_binding": {"kind": "byPath", "path": "../Revenue.SemanticModel", "resolves_in_package": True},
        "contents": {"files": {name: hashlib.sha256(content).hexdigest() for name, content in raw.items()}},
    }
    raw["package-manifest.json"] = _w_json(manifest)
    for name, content in raw.items():
        _w_put(root, name, content)
    return root, raw


def _w_bytes(package: Path) -> dict[str, bytes]:
    """Test-only reads of the synthetic ordinary tree, never used after introducing a link."""
    return {path.relative_to(package).as_posix(): path.read_bytes() for path in package.rglob("*") if path.is_file()}


def _w_revision(raw: dict[str, bytes], cache: str = _W_CACHE) -> str:
    """Literal independent R oracle over retained fixture bytes, not the production revision reader."""
    digest = hashlib.sha256()
    for name, content in sorted(raw.items()):
        if name != cache and not name.startswith("validation/iterations/"):
            digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(content).hexdigest().encode("ascii") + b"\n")
    return "sha256:" + digest.hexdigest()


def _w_redeclare(package: Path, *names: str) -> None:
    """Explicit test co-edit, never silently discover/reseal the rest of the package."""
    manifest = json.loads((package / "package-manifest.json").read_bytes())
    for name in names:
        manifest["contents"]["files"][name] = hashlib.sha256(
            package.joinpath(*name.split("/")).read_bytes()
        ).hexdigest()
    (package / "package-manifest.json").write_bytes(_w_json(manifest))


def _w_read(package: Path, current: str) -> pri.CurrentWorkingSourceDataHandoff:
    code, handoff = pri.read_current_source_data_handoff(package, expected_package_working_revision=current)
    assert code is None, code
    assert type(handoff) is pri.CurrentWorkingSourceDataHandoff
    assert pri.validate_current_source_data_handoff(package, handoff, expected_package_working_revision=current) is None
    return handoff


def test_current_working_public_interface_has_only_the_approved_required_inputs() -> None:
    """No historical digest, token, receipt, provider or policy substitution channel."""
    for function, expected in (
        (pri.read_current_source_data_handoff, ("root", "expected_package_working_revision")),
        (pri.validate_current_source_data_handoff, ("root", "handoff", "expected_package_working_revision")),
    ):
        parameters = inspect.signature(function).parameters
        assert tuple(parameters) == expected
        assert parameters["expected_package_working_revision"].kind is inspect.Parameter.KEYWORD_ONLY
        assert all(parameter.default is inspect.Parameter.empty for parameter in parameters.values())


@pytest.mark.parametrize("value", ["sha256:" + "g" * 64])
def test_current_working_revision_arguments_are_exact_before_any_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: object
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("invalid arguments reached the filesystem")

    monkeypatch.setattr(pri.revision, "tree_files", forbidden)
    assert pri.read_current_source_data_handoff(tmp_path, expected_package_working_revision=value) == (
        "working_revision_invalid",
        None,
    )
    assert (
        pri.validate_current_source_data_handoff(tmp_path, None, expected_package_working_revision=value)
        == "working_revision_invalid"
    )


def test_current_working_foreign_and_wrong_selected_cache_revision_refuse(tmp_path: Path) -> None:
    package, raw = _w_package(tmp_path / "Unit")
    _w_put(package, _W_CACHE, b"cache")
    _w_put(package, f"{_W_MODEL}/.pbi/unappliedChanges.json", b"working bytes")
    current_raw = _w_bytes(package)
    current = _w_revision(current_raw)
    _w_read(package, current)
    foreign = _w_revision({**current_raw, "data/orders.csv": b"foreign"})
    wrong_cache = _w_revision(current_raw, cache=f"{_W_MODEL}/.pbi/unappliedChanges.json")
    for value in (_w_revision(raw), foreign, wrong_cache, "sha256:" + "0" * 64):
        assert value != current
        assert pri.read_current_source_data_handoff(package, expected_package_working_revision=value) == (
            "working_revision_mismatch",
            None,
        )


@pytest.mark.parametrize("brief", [b"Plain historical prose\n", b'+++\nnumeric_obligation = "unknown"\n+++\n', b"\xff"])
def test_current_working_brief_is_physical_only_and_semantic_policy_remains_independent(
    tmp_path: Path, brief: bytes
) -> None:
    package, _ = _w_package(tmp_path / "Unit")
    _w_put(package, "migration-brief.md", brief)
    _w_redeclare(package, "migration-brief.md")
    handoff = _w_read(package, _w_revision(_w_bytes(package)))
    assert next(row[4] for row in handoff._snapshot.members if row[0] == "migration-brief.md") == brief
    code, policy = pri.read_current_brief_policy(package)
    assert code is not None and policy is None
    assert not hasattr(handoff, "brief_policy")


@pytest.mark.parametrize(
    "change,code",
    [
        ("not-utf8", "package_manifest_not_utf8"),
        ("not-json", "package_manifest_not_json"),
        ("not-object", "package_manifest_not_object"),
        ("duplicate", "package_manifest_duplicate_key"),
        ("nonfinite", "package_manifest_non_finite_number"),
        ("overflow", "package_manifest_non_finite_number"),
        ("files-type", "package_contents_files_not_object"),
        ("no-contents", "package_contents_missing"),
        ("unsafe-key", "package_declared_key_unsafe"),
        ("alias", "package_declared_keys_collide"),
        ("self-key", "package_declared_key_aliases_the_manifest"),
        ("digest-type", "package_declared_digest_not_a_string"),
        ("digest-case", "package_declared_digest_malformed"),
        ("artifacts-type", "identity_type_invalid"),
        ("unit-type", "identity_type_invalid"),
        ("unit-path", "identity_type_invalid"),
        ("kind-type", "identity_type_invalid"),
        ("unknown-kind", "package_kind_unclassified"),
    ],
)
def test_current_working_strict_manifest_refusals_are_fixed_and_shareable(
    tmp_path: Path, change: str, code: str
) -> None:
    """The malformed current declaration cannot acquire a handoff even with a matching current R."""
    package, raw = _w_package(tmp_path / "private-root")
    manifest = json.loads(raw["package-manifest.json"])
    malformed = {
        "not-utf8": b"\xffprivate-text",
        "not-json": b'{"private-text":',
        "not-object": b"[]",
        "duplicate": b'{"private-text":0,"private-text":1}',
        "nonfinite": b'{"private-text":NaN}',
        "overflow": b'{"private-text":1e999}',
    }
    if change not in malformed:
        if change == "files-type":
            manifest["contents"]["files"] = []
        elif change == "no-contents":
            del manifest["contents"]
        elif change in ("unsafe-key", "alias", "self-key"):
            key = {
                "unsafe-key": "../private-outside",
                "alias": "MIGRATION-SPEC.JSON",
                "self-key": "package-manifest.json",
            }[change]
            manifest["contents"]["files"][key] = "0" * 64
        elif change in ("digest-type", "digest-case"):
            manifest["contents"]["files"]["migration-spec.json"] = 1 if change == "digest-type" else "A" * 64
        elif change == "artifacts-type":
            manifest["artifacts"] = []
        elif change in ("unit-type", "unit-path"):
            manifest["unit"] = True if change == "unit-type" else "../private-unit"
        else:
            manifest["kind"] = True if change == "kind-type" else "private-unknown"
    content = malformed[change] if change in malformed else _w_json(manifest)
    _w_put(package, "package-manifest.json", content)
    result = pri.read_current_source_data_handoff(
        package, expected_package_working_revision=_w_revision({**raw, "package-manifest.json": content})
    )
    assert result == (code, None)
    assert "private" not in repr(result) and str(tmp_path) not in repr(result)


@pytest.mark.parametrize("name", ["source-provenance.json"])
@pytest.mark.parametrize(
    "content,code",
    [
        (b'{"duplicate":0,"duplicate":1}', "package_manifest_duplicate_key"),
        (b'{"notfinite":NaN}', "package_manifest_non_finite_number"),
        (b"\xff", "package_manifest_not_utf8"),
        (b"[", "package_manifest_not_json"),
    ],
)
def test_current_working_held_json_uses_strict_current_semantics(
    tmp_path: Path, name: str, content: bytes, code: str
) -> None:
    """A digest-matching current member still owes strict parsing."""
    package, _ = _w_package(tmp_path / "Unit")
    _w_put(package, name, content)
    _w_redeclare(package, name)
    assert pri.read_current_source_data_handoff(
        package, expected_package_working_revision=_w_revision(_w_bytes(package))
    ) == (code, None)


@pytest.mark.parametrize(
    "content",
    [b"\xff", b'{"schema":"phase1-data-access/v1","schema":"private"}', b'{"state":NaN}', b"[]", b'{"private":'],
)
def test_current_working_bad_projection_never_falls_back(tmp_path: Path, content: bytes) -> None:
    """Only the canonical projection parser owns its typed refusal."""
    package, _ = _w_package(tmp_path / "Unit")
    _w_put(package, "data-access.json", content)
    _w_redeclare(package, "data-access.json")
    assert pri.read_current_source_data_handoff(
        package, expected_package_working_revision=_w_revision(_w_bytes(package))
    ) == ("projection-invalid", None)


@pytest.mark.parametrize(
    "declaration", ["asset", "migration_spec", "migration_spec_schema", "data_access", "migration_brief"]
)
@pytest.mark.parametrize("change", ["absent", "wrong-path", "no-digest"])
def test_current_working_roles_are_exact_declarations_not_name_discovery(
    tmp_path: Path, declaration: str, change: str
) -> None:
    """Tempting correctly named files never restore a missing or foreign role declaration."""
    package, raw = _w_package(tmp_path / "Unit")
    manifest = json.loads(raw["package-manifest.json"])
    name = manifest["artifacts"][declaration]
    if change == "absent":
        del manifest["artifacts"][declaration]
        code = "role_declaration_absent"
    elif change == "wrong-path":
        manifest["artifacts"][declaration] = "data/orders.csv"
        code = "role_declaration_not_an_admissible_candidate"
    else:
        del manifest["contents"]["files"][name]
        code = "role_declaration_not_a_verified_file"
    _w_put(package, "package-manifest.json", _w_json(manifest))
    assert pri.read_current_source_data_handoff(
        package, expected_package_working_revision=_w_revision(_w_bytes(package))
    ) == (code, None)


@pytest.mark.parametrize(
    "change,code",
    [
        ("classification-scope", "package_scope_mismatch"),
        ("classification-kind", "engine_classification_membership"),
        ("classification-duplicate", "engine_classification_membership"),
        ("provenance-scope", "package_scope_mismatch"),
        ("provenance-row", "provenance_row_cardinality"),
        ("provenance-digest", "provenance_sha_disagrees_with_source"),
        ("label-empty", "identity_type_invalid"),
        ("label-absent", "identity_type_invalid"),
        ("spec-file", "spec_file_disagrees_with_source"),
        ("luid-namespace", "luid_namespace_mismatch"),
        ("luid-mismatch", "server_luid_contradiction"),
        ("luid-agrees-only-with-label", "server_luid_contradiction"),
        ("luid-missing", "server_luid_unrepresented"),
        ("source-extension", "role_declaration_not_an_admissible_candidate"),
        ("source-extra", "role_ambiguous"),
    ],
)
def test_current_working_pure_identity_rules_refuse_coherent_digests_with_wrong_roles(
    tmp_path: Path, change: str, code: str
) -> None:
    """Current digests alone cannot replace source/provenance/LUID/classification agreement."""
    package, raw = _w_package(tmp_path / "Unit")
    name = "report.json" if change.startswith("classification") else "source-provenance.json"
    payload = json.loads(raw[name])
    if change == "classification-scope":
        payload["scope"]["unit"] = "Other"
    elif change == "classification-kind":
        payload["datasources"], payload["workbooks"] = payload["workbooks"], []
    elif change == "classification-duplicate":
        payload["workbooks"] *= 2
    elif change == "provenance-scope":
        payload["scope"]["unit"] = "Other"
    elif change == "provenance-row":
        payload["inputs"] *= 2
    elif change == "provenance-digest":
        payload["inputs"][0]["input"]["sha256"] = "0" * 64
    elif change == "label-empty":
        payload["inputs"][0]["input"]["file"] = ""
    elif change == "label-absent":
        del payload["inputs"][0]["input"]["file"]
    elif change == "spec-file":
        name, payload = "migration-spec.json", json.loads(raw["migration-spec.json"])
        payload["source"]["file_name"] = "Other.twb"
    elif change == "luid-namespace":
        payload["inputs"][0]["origin"]["datasource_luid"] = DS_LUID
    elif change == "luid-mismatch":
        payload["inputs"][0]["origin"]["workbook_luid"] = DS_LUID
    elif change == "luid-agrees-only-with-label":
        payload["inputs"][0]["origin"]["workbook_luid"] = DS_LUID
        payload["inputs"][0]["input"]["file"] = f"{DS_LUID}_Revenue.twb"
    elif change == "luid-missing":
        payload["inputs"][0]["origin"] = {}
    elif change == "source-extra":
        _w_put(package, "assets/extra.bin", b"extra")
        _w_redeclare(package, "assets/extra.bin")
    else:
        name = "package-manifest.json"
        payload = json.loads(raw[name])
        replacement = _W_ASSET.removesuffix(".twb") + ".tds"
        package.joinpath(*_W_ASSET.split("/")).rename(package.joinpath(*replacement.split("/")))
        payload["artifacts"]["asset"] = replacement
        payload["contents"]["files"][replacement] = payload["contents"]["files"].pop(_W_ASSET)
    if change != "source-extra":
        _w_put(package, name, _w_json(payload))
        if name != "package-manifest.json":
            _w_redeclare(package, name)
    assert pri.read_current_source_data_handoff(
        package, expected_package_working_revision=_w_revision(_w_bytes(package))
    ) == (code, None)


@pytest.mark.parametrize("label", [f"{DS_LUID}_Revenue.twb", "Revenue.twb"], ids=["foreign-luid", "no-luid"])
def test_current_working_provenance_file_is_a_label_not_identity(tmp_path: Path, label: str) -> None:
    """#865: compact-ID staging renames the asset; sha256 plus the ASSET-name LUID still identify it."""
    package, raw = _w_package(tmp_path / "Unit")
    payload = json.loads(raw["source-provenance.json"])
    payload["inputs"][0]["input"]["file"] = label
    _w_put(package, "source-provenance.json", _w_json(payload))
    _w_redeclare(package, "source-provenance.json")
    _w_read(package, _w_revision(_w_bytes(package)))


@pytest.mark.parametrize(
    "change,code",
    [
        ("datasource", "working_topology_unsupported"),
        ("provider", "working_topology_unsupported"),
        ("shared-dependency", "working_topology_unsupported"),
        ("unidentified-dependency", "published_dependency_invalid"),
        ("report-free", "role_declaration_absent"),
        ("multiple-model", "role_ambiguous"),
        ("multiple-report", "role_ambiguous"),
        ("multiple-pbip", "role_ambiguous"),
        ("external-binding", "provider_binding_mismatch"),
        ("byConnection", "provider_binding_mismatch"),
        ("pbip-target", "provider_binding_mismatch"),
        ("pbip-targets", "provider_binding_mismatch"),
        ("summary-target", "provider_binding_mismatch"),
        ("summary-local", "provider_binding_mismatch"),
        ("model-path", "role_declaration_not_a_verified_file"),
        ("unowned-sqlproxy", "source-key-invalid"),
        ("empty-second-model", "working_topology_unsupported"),
        ("nested-model", "working_topology_unsupported"),
        ("nested-report", "working_topology_unsupported"),
        ("nested-pbip", "working_topology_unsupported"),
        ("root-pbip", "working_topology_unsupported"),
    ],
)
def test_current_working_unsupported_or_incoherent_targets_never_issue(tmp_path: Path, change: str, code: str) -> None:
    """Only the single owned report/model/PBIP target is in W's success topology."""
    package, raw = _w_package(tmp_path / "Unit")
    manifest = json.loads(raw["package-manifest.json"])
    if change in ("datasource", "provider"):
        manifest["kind"] = "datasource"
    elif change in ("shared-dependency", "unidentified-dependency", "unowned-sqlproxy"):
        spec = json.loads(raw["migration-spec.json"])
        spec["data_sources"][0]["connection"] = {"class": "sqlproxy", "mode": "live"}
        if change != "unowned-sqlproxy":
            spec["data_sources"][0]["published_datasource"] = {"luid": DS_LUID} if change == "shared-dependency" else {}
        _w_put(package, "migration-spec.json", _w_json(spec))
        manifest["contents"]["files"]["migration-spec.json"] = hashlib.sha256(_w_json(spec)).hexdigest()
    elif change == "report-free":
        manifest["artifacts"]["report"] = None
    elif change.startswith("multiple"):
        name = {
            "multiple-model": "fabric/Other.SemanticModel/definition/model.tmdl",
            "multiple-report": "fabric/Other.Report/definition.pbir",
            "multiple-pbip": "fabric/Other.pbip",
        }[change]
        _w_put(package, name, b"other")
        manifest["contents"]["files"][name] = hashlib.sha256(b"other").hexdigest()
    elif change == "empty-second-model":
        (package / "fabric" / "Other.SemanticModel").mkdir()
    elif change in ("nested-model", "nested-report", "nested-pbip", "root-pbip"):
        name = {
            "nested-model": "fabric/nested/Other.SemanticModel/definition/model.tmdl",
            "nested-report": "fabric/nested/Other.Report/definition.pbir",
            "nested-pbip": "fabric/nested/Other.pbip",
            "root-pbip": "Other.pbip",
        }[change]
        _w_put(package, name, b"other target")
    elif change in ("external-binding", "byConnection"):
        reference = (
            {"byPath": {"path": "../../../external.SemanticModel"}}
            if change == "external-binding"
            else {"byConnection": {}}
        )
        _w_put(package, f"{_W_REPORT}/definition.pbir", _w_json({"datasetReference": reference}))
    elif change in ("pbip-target", "pbip-targets"):
        artifacts = (
            [{"report": {"path": "Other.Report"}}]
            if change == "pbip-target"
            else [
                {"report": {"path": "Revenue.Report"}},
                {"report": {"path": "Revenue.Report"}},
            ]
        )
        _w_put(package, _W_PBIP, _w_json({"artifacts": artifacts}))
    elif change in ("summary-target", "summary-local"):
        manifest["model_binding"]["path" if change == "summary-target" else "resolves_in_package"] = (
            "../Other.SemanticModel" if change == "summary-target" else False
        )
    else:
        manifest["artifacts"]["model"] = "fabric/Other.SemanticModel"
    _w_put(package, "package-manifest.json", _w_json(manifest))
    assert pri.read_current_source_data_handoff(
        package, expected_package_working_revision=_w_revision(_w_bytes(package))
    ) == (code, None)


@pytest.mark.parametrize("mode", ["subclass"])
def test_current_working_root_arguments_are_exact_native_paths(tmp_path: Path, mode: str) -> None:
    """No argument coercion, normalization or filesystem fallback."""
    root = str(tmp_path) if mode == "string" else _WorkingPath(tmp_path) if mode == "subclass" else True
    assert pri.read_current_source_data_handoff(root, expected_package_working_revision="sha256:" + "0" * 64) == (
        "package_root_binding_invalid",
        None,
    )
    assert (
        pri.validate_current_source_data_handoff(root, None, expected_package_working_revision="sha256:" + "0" * 64)
        == "package_root_binding_invalid"
    )


@pytest.mark.parametrize("change", ["equal", "assessment", "facts", "source", "scalar", "snapshot"])
def test_current_working_module_cannot_remint_reconstructed_or_upgraded_handoffs(tmp_path: Path, change: str) -> None:
    """The review's module-factory attack must not upgrade a real held blocked projection."""
    blocked = {
        **_W_LOCAL,
        "state": "blocked",
        "validation": "not_established",
        "effective_scope": None,
        "max_phase2_claim": "none",
        "codes": ["probe-no-credential"],
    }
    package, raw = _w_package(tmp_path / "Unit", live=True, projection=blocked)
    current = _w_revision(raw)
    handoff = _w_read(package, current)
    assert handoff.stored_data_access.to_json() == blocked
    values = {item.name: getattr(handoff, item.name) for item in fields(handoff) if item.init}
    if change == "assessment":
        values["stored_data_access"] = pri.parse_data_access(
            json.dumps(
                {
                    **_W_LOCAL,
                    "state": "live_data_ok",
                    "source_keys": [_W_LIVE_KEY],
                    "codes": ["probe-cleared", "probe-data-ok"],
                }
            )
        )
        assert values["stored_data_access"].max_phase2_claim == "data_validated"
    elif change == "facts":
        values["facts"] = handoff.facts._replace(live_source_keys=())
    elif change == "source":
        values["source_identity"] = replace(handoff.source_identity, sha256="0" * 64)
    elif change == "scalar":
        values["unit"] = "Other"
    elif change == "snapshot":
        values["_snapshot"] = copy.deepcopy(handoff._snapshot)
    candidate = pri.CurrentWorkingSourceDataHandoff(**values)
    assert candidate is not handoff
    assert (
        pri.validate_current_source_data_handoff(package, candidate, expected_package_working_revision=current)
        == "working_handoff_invalid"
    )
    factory = getattr(pri, "_working_authority", None)
    if factory is not None:
        object.__setattr__(candidate, "_authority", factory(candidate))
    actual = pri.validate_current_source_data_handoff(package, candidate, expected_package_working_revision=current)
    assert actual == "working_handoff_invalid", (
        change,
        handoff.stored_data_access.state,
        candidate.stored_data_access.state,
        actual,
    )
    assert pri.validate_current_source_data_handoff(package, handoff, expected_package_working_revision=current) is None


def test_current_working_issuance_has_no_module_factory_or_global_registry() -> None:
    """Read-only introspection checks the supported module surface, not hostile closure rewriting."""
    assert not hasattr(pri, "_working_authority")
    assert not hasattr(pri, "_WORKING_AUTHORITY_CODE")
    assert not hasattr(pri, "_current_working_api")
    issuing = inspect.getclosurevars(pri.read_current_source_data_handoff).nonlocals["issued"]
    validating = inspect.getclosurevars(pri.validate_current_source_data_handoff).nonlocals["issued"]
    assert issuing is validating and isinstance(issuing, weakref.WeakValueDictionary)
    assert all(value is not issuing for value in vars(pri).values())


@pytest.mark.parametrize("mode", ["lambda"])
def test_current_working_replacement_capabilities_are_not_invoked(tmp_path: Path, mode: str) -> None:
    package, raw = _w_package(tmp_path / "Unit")
    current = _w_revision(raw)
    handoff = _w_read(package, current)

    class Permissive:
        def __call__(self, _candidate: object) -> bool:
            pytest.fail("validation invoked a caller-supplied capability")

    authority = handoff._authority
    replacement = (
        (lambda _candidate: True)
        if mode == "lambda"
        else Permissive()
        if mode == "callable"
        else types.FunctionType(authority.__code__, authority.__globals__, closure=authority.__closure__)
    )
    object.__setattr__(handoff, "_authority", replacement)
    assert (
        pri.validate_current_source_data_handoff(package, handoff, expected_package_working_revision=current)
        == "working_handoff_invalid"
    )


@pytest.mark.parametrize("cycle", ["none", "scalar", "members"])
def test_current_working_disposed_issuance_and_cycles_are_collectible(tmp_path: Path, cycle: str) -> None:
    package, raw = _w_package(tmp_path / "Unit")
    current = _w_revision(raw)
    handoff = _w_read(package, current)
    candidate = copy.deepcopy(replace(handoff))
    owner, capability = weakref.ref(handoff), weakref.ref(handoff._authority)
    key = id(handoff)
    issued = inspect.getclosurevars(pri.read_current_source_data_handoff).nonlocals["issued"]
    assert issued[key] is handoff._authority
    if cycle != "none":
        if cycle == "scalar":
            object.__setattr__(handoff, "unit", handoff)
        else:
            object.__setattr__(handoff._snapshot, "members", (handoff,))
        assert (
            pri.validate_current_source_data_handoff(package, handoff, expected_package_working_revision=current)
            == "working_handoff_invalid"
        )
    del handoff
    gc.collect()
    assert owner() is None and capability() is None and key not in issued
    for _ in range(256):
        allocated = replace(candidate)
        assert (
            pri.validate_current_source_data_handoff(package, allocated, expected_package_working_revision=current)
            == "working_handoff_invalid"
        )
    _w_read(package, current)


@pytest.mark.parametrize(
    "field_name",
    [
        "package_root",
        "root_identity",
        "package_working_revision",
        "current_manifest_sha256",
        "unit",
        "kind",
        "topology",
        "source_asset_path",
        "report_path",
        "model_path",
        "pbip_path",
        "declared_paths",
        "source_identity",
        "facts",
        "stored_data_access",
        "_snapshot",
        "_authority",
        "source-value",
        "member-value",
        "manifest-value",
        "boundary-value",
        "scalar-subclass",
        "member-subclass",
        "permissive-authority",
        "cycle-scalar",
        "cycle-source",
        "cycle-members",
    ],
)
def test_current_working_issued_state_mutations_and_grafts_refuse(tmp_path: Path, field_name: str) -> None:
    """Mutating even the original frozen object cannot silently change the held authority."""
    package, raw = _w_package(tmp_path / "Unit")
    current = _w_revision(raw)
    handoff = _w_read(package, current)
    other = _w_read(package, current)
    if field_name in ("source_identity", "facts", "stored_data_access", "_snapshot"):
        replacement = getattr(other, field_name)
    elif field_name == "package_root":
        replacement = tmp_path
    elif field_name == "declared_paths":
        replacement = (*handoff.declared_paths, "rogue.bin")
    elif field_name == "_authority":
        replacement = None
    elif field_name == "permissive-authority":
        field_name, replacement = "_authority", lambda _candidate: True
    elif field_name == "source-value":
        object.__setattr__(handoff.source_identity, "sha256", "0" * 64)
        replacement = None
    elif field_name == "cycle-scalar":
        object.__setattr__(handoff, "unit", handoff)
        replacement = None
    elif field_name == "cycle-source":
        object.__setattr__(handoff.source_identity, "kind", handoff.source_identity)
        replacement = None
    elif field_name == "cycle-members":
        object.__setattr__(handoff._snapshot, "members", (handoff._snapshot,))
        replacement = None
    elif field_name in ("member-value", "manifest-value", "boundary-value"):
        snapshot = handoff._snapshot
        attribute, value = {
            "member-value": ("members", snapshot.members[:-1]),
            "manifest-value": ("manifest", snapshot.manifest + b"\n"),
            "boundary-value": ("boundary", ()),
        }[field_name]
        object.__setattr__(snapshot, attribute, value)
        replacement = None
    elif field_name == "scalar-subclass":
        field_name, replacement = "unit", _WorkingString(handoff.unit)
    elif field_name == "member-subclass":
        field_name, replacement = "declared_paths", tuple(_WorkingString(name) for name in handoff.declared_paths)
    else:
        replacement = "private-mutated-text"
    if replacement is not None or field_name == "_authority":
        object.__setattr__(handoff, field_name, replacement)
    assert (
        pri.validate_current_source_data_handoff(package, handoff, expected_package_working_revision=current)
        == "working_handoff_invalid"
    )
    assert "private-mutated-text" not in repr(handoff)


def test_current_working_root_and_revision_grafts_refuse_but_fresh_transfers_are_snapshots(tmp_path: Path) -> None:
    """The same bytes can be freshly reviewed elsewhere, never by borrowing another root's object."""
    package, raw = _w_package(tmp_path / "Unit")
    current = _w_revision(raw)
    handoff = _w_read(package, current)
    destination = tmp_path / "Copy"
    shutil.copytree(package, destination)
    assert _w_bytes(destination) == raw
    assert (
        pri.validate_current_source_data_handoff(destination, handoff, expected_package_working_revision=current)
        == "package_root_binding_invalid"
    )
    _w_read(destination, current)
    assert (
        pri.validate_current_source_data_handoff(
            package, handoff, expected_package_working_revision="sha256:" + "0" * 64
        )
        == "working_revision_mismatch"
    )
    with pytest.raises(AttributeError):
        handoff.facts.live_source_keys = (_W_OTHER_KEY,)
    with pytest.raises(AttributeError):
        handoff.stored_data_access.max_phase2_claim = "structural_only"


@pytest.mark.parametrize("suffix", [".report", ".semanticmodel"])
@pytest.mark.parametrize("parent", ["fabric", "nested"])
@pytest.mark.parametrize("populated", [False, True], ids=["empty", "nonempty"])
def test_current_working_suffix_variants_refuse_fresh_reads_with_matching_r(
    tmp_path: Path, suffix: str, parent: str, populated: bool
) -> None:
    package, raw = _w_package(tmp_path / "Unit")
    extra = f"{parent}/Other{suffix}"
    package.joinpath(*extra.split("/")).mkdir(parents=True)
    if populated:
        raw[f"{extra}/payload.bin"] = b"other target"
        _w_put(package, f"{extra}/payload.bin", raw[f"{extra}/payload.bin"])
    current = _w_revision(raw)
    assert pri.revision.package_working_revision(package, package / _W_MODEL) == current
    if sys.platform == "win32":
        discover = bundle_corpus.shipping_reports if suffix.casefold() == ".report" else bundle_corpus.shipping_models
        selected = "Revenue.Report" if suffix.casefold() == ".report" else "Revenue.SemanticModel"
        assert {path.name for path in discover(package)} == {selected, f"Other{suffix}"}
    expected = "role_ambiguous" if populated and parent == "fabric" else "working_topology_unsupported"
    assert pri.read_current_source_data_handoff(package, expected_package_working_revision=current) == (expected, None)


@pytest.mark.parametrize(
    "suffix", [".report", ".REPORT", ".rEpOrT", ".semanticmodel", ".SEMANTICMODEL", ".sEmAnTiCmOdEl"]
)
@pytest.mark.parametrize(
    ("parent", "populated"), [("fabric", False), ("nested", True)], ids=["fabric-empty", "nested-nonempty"]
)
def test_current_working_suffix_variants_refuse_held_validation_at_its_own_r(
    tmp_path: Path, suffix: str, parent: str, populated: bool
) -> None:
    """Nonempty additions must fail on topology before stale bytes could incidentally refuse."""
    package, raw = _w_package(tmp_path / "Unit")
    current = _w_revision(raw)
    handoff = _w_read(package, current)
    extra = f"{parent}/Other{suffix}"
    package.joinpath(*extra.split("/")).mkdir(parents=True)
    if populated:
        _w_put(package, f"{extra}/payload.bin", b"other target")
    else:
        assert _w_bytes(package) == raw
        assert pri.revision.package_working_revision(package, package / _W_MODEL) == current
    assert handoff.package_working_revision == current
    assert (
        pri.validate_current_source_data_handoff(package, handoff, expected_package_working_revision=current)
        == "working_topology_unsupported"
    )


@pytest.mark.parametrize("name", ["package-manifest.json", "source-provenance.json"])
def test_current_working_same_byte_replacement_after_issue_refuses(tmp_path: Path, name: str) -> None:
    """R is unchanged, so the physical member identity assertion must be the refusal."""
    package, raw = _w_package(tmp_path / "Unit")
    current = _w_revision(raw)
    handoff = _w_read(package, current)
    path = package.joinpath(*name.split("/"))
    previous = os.lstat(path).st_ino
    path.rename(tmp_path / "retired")
    path.write_bytes(raw[name])
    assert os.lstat(path).st_ino != previous and _w_revision(_w_bytes(package)) == current
    expected = "package_root_replaced" if name == "package-manifest.json" else "package_member_replaced"
    assert (
        pri.validate_current_source_data_handoff(package, handoff, expected_package_working_revision=current)
        == expected
    )


@pytest.mark.parametrize("boundary", ["root", "ancestor", "asset-directory", "marker", "spec", "projection"])
def test_current_working_links_refuse_before_any_content_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    """Real NTFS junctions/POSIX symlinks; only the existing governed capability skips apply."""
    package, raw = _w_package(tmp_path / "outside" / "Unit")
    if boundary in ("root", "ancestor"):
        link = tmp_path / "alias"
        link_directory(link, package if boundary == "root" else package.parent)
        package = link if boundary == "root" else link / package.name
    elif boundary == "asset-directory":
        path = package / "assets"
        target = tmp_path / "retired-assets"
        path.rename(target)
        link_directory(path, target)
    else:
        name = {"marker": "package-manifest.json", "spec": "migration-spec.json", "projection": "data-access.json"}[
            boundary
        ]
        path = package / name
        target = tmp_path / "retired-file"
        path.rename(target)
        link_file(path, target)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a reparse boundary was opened")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(pri.pfs, "_hash_file", forbidden)
    assert pri.read_current_source_data_handoff(package, expected_package_working_revision=_w_revision(raw)) == (
        "package_boundary_unsafe",
        None,
    )


@pytest.mark.parametrize("name", ["package-manifest.json", "source-provenance.json"])
def test_current_working_hardlinks_refuse_before_role_bytes_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The ordinary-single-link primitive closes the gap left by a no-follow walk alone."""
    package, raw = _w_package(tmp_path / "Unit")
    path = package.joinpath(*name.split("/"))
    os.link(path, tmp_path / "another-name")
    assert os.lstat(path).st_nlink == 2
    read, stream = Path.read_bytes, pri.pfs._hash_file

    def checked_read(candidate: Path) -> bytes:
        assert candidate != path, "hardlinked role bytes were opened"
        return read(candidate)

    def checked_hash(candidate: Path) -> str:
        assert candidate != path, "hardlinked role bytes were hashed"
        return stream(candidate)

    monkeypatch.setattr(Path, "read_bytes", checked_read)
    monkeypatch.setattr(pri.pfs, "_hash_file", checked_hash)
    code = "package_marker_replaced" if name == "package-manifest.json" else "package_member_replaced"
    assert pri.read_current_source_data_handoff(package, expected_package_working_revision=_w_revision(raw)) == (
        code,
        None,
    )


@pytest.mark.parametrize("when", [1, 2])
@pytest.mark.parametrize("change", ["replace-member", "edit-member", "replace-marker", "edit-marker", "edit-report"])
def test_current_working_revision_read_seams_recheck_held_identity_and_current_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: int, change: str
) -> None:
    """A returning revision value cannot conceal the named change during the held-read interval."""
    package, raw = _w_package(tmp_path / "Unit")
    current = _w_revision(raw)
    revision_reader = pri.revision.package_working_revision
    calls = []
    name = "package-manifest.json" if change.endswith("marker") else "migration-spec.json"
    if change == "edit-report":
        name = f"{_W_REPORT}/definition/pages/pages.json"
    path = package.joinpath(*name.split("/"))

    def change_at_return(root: Path, model: Path | None = None) -> str:
        result = revision_reader(root, model)
        calls.append(result)
        if len(calls) == when:
            if change.startswith("replace"):
                path.rename(tmp_path / "retired")
                path.write_bytes(raw[name])
            else:
                path.write_bytes(raw[name] + b"\n")
        return result

    monkeypatch.setattr(pri.revision, "package_working_revision", change_at_return)
    code, handoff = pri.read_current_source_data_handoff(package, expected_package_working_revision=current)
    assert len(calls) >= when and calls[0] == current
    if change == "edit-report" and when == 2:
        # No guarantee about a non-held mutable file changed AFTER the final revision read.
        # A subsequent consumer validation must still refuse the old object/revision.
        assert code is None and handoff is not None
        assert (
            pri.validate_current_source_data_handoff(package, handoff, expected_package_working_revision=current)
            == "working_revision_mismatch"
        )
    else:
        assert handoff is None
        expected = (
            "package_root_replaced"
            if change == "replace-marker"
            else "package_member_replaced"
            if change == "replace-member"
            else "working_revision_mismatch"
            if change == "edit-report" or (change == "edit-marker" and when == 1)
            else "package_file_digest_mismatch"
        )
        assert code == expected


def test_current_working_reader_invokes_no_baseline_gate_receipt_probe_policy_or_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forbidden-owner sentinels surround a real positive and its real revalidation."""
    import subprocess

    import check_reference_readiness as readiness
    import iteration_receipt as receipts
    import package_data_access as pda

    package, raw = _w_package(tmp_path / "Unit")
    _w_put(package, f"{_W_REPORT}/definition/pages/pages.json", b'{"pageOrder":["working"]}')
    _w_put(package, "validation/iterations/anything.bin", b"not evidence")
    _w_put(package, _W_CACHE, b"not evidence")
    before = _w_bytes(package)
    current = _w_revision(before)
    reads = Path.read_bytes

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("W invoked a forbidden authority or writer")

    def small_reads(path: Path) -> bytes:
        assert path != package.joinpath(*_W_ASSET.split("/")), "large source assets must be streaming-hashed"
        return reads(path)

    for module, names in (
        (
            pri,
            (
                "verify_s1",
                "verify_phase1_role_identity",
                "_facts",
                "_verdict",
                "_resolve_cohort",
                "VerifiedPackage",
                "_Facts",
                "Phase1RoleIdentityResult",
                "PackageDataAccessHandoff",
                "parse_brief_policy",
                "read_current_brief_policy",
            ),
        ),
        (pri.pfs, ("verify_package", "read_verified_member", "HeldVerifiedMember")),
        (readiness, ("scan", "_entry_integrity")),
        (receipts, ("read_chain", "read_history", "write_receipt", "finalize")),
        (pda, ("reconcile_package_data_access",)),
        (subprocess, ("run", "Popen")),
        (Path, ("write_bytes", "write_text", "mkdir")),
    ):
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(Path, "read_bytes", small_reads)
    handoff = _w_read(package, current)
    assert handoff.current_manifest_sha256 == hashlib.sha256(raw["package-manifest.json"]).hexdigest()
    monkeypatch.undo()
    assert _w_bytes(package) == before


@pytest.mark.parametrize("name", ["package-manifest.json", "migration-spec.json"])
def test_current_working_unreadable_small_roles_return_no_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """IO failures have fixed reasons, without dumping a filename or the original exception."""
    package, raw = _w_package(tmp_path / "Unit")
    read_bytes = Path.read_bytes

    def deny(path: Path) -> bytes:
        if path.name == name:
            raise OSError("private source location and diagnostic")
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", deny)
    assert pri.read_current_source_data_handoff(package, expected_package_working_revision=_w_revision(raw)) == (
        "package_file_unreadable",
        None,
    )


# --------------------------------------------------------------------------------------------
# S2 review reproductions (formerly test_package_role_identity_reproductions.py)
# --------------------------------------------------------------------------------------------


def reseal(package: Path) -> None:
    """Keep S1 clean while changing an S2 claim."""
    seal(package, **json.loads((package / "package-manifest.json").read_text(encoding="utf-8")))


def replace_field(package: Path, file: str, keys: tuple[str | int, ...], value: Any) -> None:
    """Change exactly one named claim and re-seal the independent fixture."""
    payload = json.loads((package / file).read_text(encoding="utf-8"))
    cursor = payload
    for key in keys[:-1]:
        cursor = cursor[key]
    cursor[keys[-1]] = value
    _write(package / file, payload)
    reseal(package)


def add_reference(package: Path) -> None:
    """An independent reference provider with the package's source SHA."""
    source = next((package / "assets").iterdir())
    _write(package / "reference" / "dashboard" / "view.png", "png-bytes")
    _write(
        package / "reference" / "manifest.json",
        {
            "source_workbook_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "dashboards": [{"name": "Overview", "states": [{"image": "dashboard/view.png"}]}],
        },
    )
    reseal(package)


def cohort(root: Path, *, provider_luid: str | None = DS_LUID) -> tuple[Path, Path]:
    """A real cross-package binding, not an unresolved basename that happens to match."""
    provider = datasource_package(root / DS_UNIT, luid=provider_luid, published_key=PUBLISHED_KEY)
    consumer = workbook_package(
        root / WB_UNIT,
        published={"key": PUBLISHED_KEY},
        binding=f"../../../{DS_UNIT}/fabric/{DS_UNIT}.SemanticModel",
    )
    return provider, consumer


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("asset", "package_integrity_not_clean"),
        ("manifest", "package_integrity_not_clean"),
        ("missing-marker", "package_boundary_not_declared"),
        ("missing-root", "package_boundary_not_declared"),
    ],
)
def test_supplied_s1_observation_cannot_admit_changed_state(tmp_path: Path, change: str, code: str) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    cleared = pri.verify_s1(package)
    assert cleared.integrity.is_clean
    assert verify_one(package).verdict == "START_READY"
    if change == "asset":
        next((package / "assets").iterdir()).write_text("changed bytes", encoding="utf-8")
    elif change == "manifest":
        (package / "package-manifest.json").write_text("{", encoding="utf-8")
    elif change == "missing-marker":
        (package / "package-manifest.json").unlink()
    else:
        package.rename(tmp_path / "retired")

    result = pri.verify_phase1_role_identity([package], verified=[cleared])[0]

    assert result.blockers == (code,), "the S2 entry seam reused stale S1 authority"
    assert result.verdict == "BLOCKED"
    assert result.roles == ()


def test_supplied_s1_observation_cannot_follow_a_replaced_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    cleared = pri.verify_s1(package)
    retired = package.rename(tmp_path / "retired")
    link_directory(package, retired)
    opened = []
    original = Path.read_bytes

    def tracked(path: Path) -> bytes:
        opened.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", tracked)
    try:
        result = pri.verify_phase1_role_identity([package], verified=[cleared])[0]
        assert result.blockers == ("package_boundary_unsafe",), "a reparse-swapped root retained clearance"
        assert opened == [], "S2 read through a replaced root before refusing it"
    finally:
        if os.name == "nt":
            package.rmdir()
        else:
            package.unlink()


@pytest.mark.parametrize("origin", ["reference", "oracle"])
@pytest.mark.parametrize("spelling", [r"dashboard\view.png", "dashboard/missing.png"])
def test_evidence_requires_an_exact_canonical_walked_member(tmp_path: Path, origin: str, spelling: str) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    if origin == "reference":
        add_reference(package)
    assert verify_one(package).verdict == "START_READY"
    filename = f"{origin}/{'manifest.json' if origin == 'reference' else 'oracle-manifest.json'}"
    keys = ("dashboards", 0, "states", 0, "image") if origin == "reference" else ("views", 0, "image", "path")
    replace_field(package, filename, keys, spelling)
    result = verify_one(package)
    found = role(result, f"tableau_{origin}")

    assert (found.state, found.code) == ("mismatch", "evidence_path_not_verified")
    assert result.verdict == "BLOCKED"
    assert result.evidence == ()
    assert spelling not in json.dumps(result.as_dict())


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        ({}, "published_dependency_identity_missing"),
        ({"id": DS_UNIT}, "published_dependency_identity_missing"),
        ({"key": []}, "published_dependency_invalid"),
        (0.5, "published_dependency_invalid"),
    ],
)
def test_invalid_published_rows_remain_counted_and_block(tmp_path: Path, published: Any, expected: str) -> None:
    provider, consumer = cohort(tmp_path)
    spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
    spec["data_sources"].append({"published_datasource": published})
    _write(consumer / "migration-spec.json", spec)
    reseal(consumer)

    result = pri.verify_phase1_role_identity([provider, consumer])[1]

    assert len(result.dependencies) == 2, "a declared row disappeared from the dependency denominator"
    assert result.dependencies[0].code == "published_dependency_authority_contradiction"
    assert (result.dependencies[1].state, result.dependencies[1].code) == ("mismatch", expected)
    assert result.topology == "published_consumer"
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize("value", [[False], 0.5])
def test_malformed_datasource_collections_are_not_owned_model_fallbacks(tmp_path: Path, value: Any) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    replace_field(package, "migration-spec.json", ("data_sources",), value)

    result = verify_one(package)

    assert len(result.dependencies) == 1
    assert result.dependencies[0].code == "published_dependency_invalid"
    assert result.topology == "published_consumer"
    assert result.verdict == "BLOCKED"


def test_spec_luid_cannot_override_acquired_authority(tmp_path: Path) -> None:
    provider, consumer = cohort(tmp_path)
    replace_field(
        consumer,
        "migration-spec.json",
        ("data_sources", 0, "published_datasource", "luid"),
        "99999999-9999-9999-9999-999999999999",
    )

    result = pri.verify_phase1_role_identity([provider, consumer])[1]

    assert result.dependencies[0].code == "published_dependency_authority_contradiction"
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize(
    "source_match,state,count,expected_state,code",
    [
        ("sha256", "cannot_establish", None, "cannot_establish", "published_dependency_authority_unestablished"),
        ("unestablished", "cannot_establish", None, "cannot_establish", "published_dependency_authority_unestablished"),
        ("revision_same", "ambiguous", 2, "ambiguous", "published_dependency_authority_ambiguous"),
    ],
)
def test_acquired_non_success_is_not_a_provider_selection_request(
    tmp_path: Path,
    source_match: str,
    state: str,
    count: int | None,
    expected_state: str,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, consumer = cohort(tmp_path)
    provenance = json.loads((consumer / "source-provenance.json").read_bytes())
    authority = provenance["inputs"][0]["origin"]["published_dependencies"]
    authority["source_match"] = source_match
    authority["rows"][0] = {
        "source_ordinal": 0,
        "published_key": PUBLISHED_KEY,
        "state": state,
        "candidate_count": count,
    }
    _write(consumer / "source-provenance.json", provenance)
    reseal(consumer)

    def forbidden(*_args: object) -> None:
        pytest.fail("non-success acquired authority reached provider selection")

    monkeypatch.setattr(pri, "_provider_matches", forbidden)
    result = pri.verify_phase1_role_identity([provider, consumer])[1]
    assert result.verdict == "BLOCKED"
    assert (result.dependencies[0].state, result.dependencies[0].code) == (expected_state, code)
    assert result.dependencies[0].provider_ordinal is None
    assert result.codes() == (code,)


@pytest.mark.parametrize(
    "keys,value,code",
    [
        (("schema",), "unknown/v1", "published_dependency_authority_invalid"),
        (("source_sha256",), True, "published_dependency_authority_invalid"),
        (("source_sha256",), "A" * 64, "published_dependency_authority_invalid"),
        (("workbook_luid",), DS_LUID, "published_dependency_authority_contradiction"),
        (("workbook_luid",), "not-a-uuid", "published_dependency_authority_invalid"),
        (("source_match",), "name_only", "published_dependency_authority_invalid"),
        (("source_match",), "unestablished", "published_dependency_authority_invalid"),
        (("private",), {}, "published_dependency_authority_invalid"),
        (("rows",), [], "published_dependency_authority_invalid"),
        (("rows",), {}, "published_dependency_authority_invalid"),
        (("rows", 0, "source_ordinal"), 1, "published_dependency_authority_contradiction"),
        (("rows", 0, "source_ordinal"), True, "published_dependency_authority_invalid"),
        (("rows", 0, "published_key"), PUBLISHED_KEY.upper(), "published_dependency_authority_contradiction"),
        (("rows", 0, "published_key"), "x" * 1025, "published_dependency_authority_invalid"),
        (("rows", 0, "state"), "missing", "published_dependency_authority_invalid"),
        (("rows", 0, "state"), "ambiguous", "published_dependency_authority_invalid"),
        (("rows", 0, "candidate_count"), 1.0, "published_dependency_authority_invalid"),
        (("rows", 0, "acquisition"), {}, "published_dependency_authority_invalid"),
    ],
)
def test_invalid_or_contradictory_authority_never_selects_a_provider(
    tmp_path: Path,
    keys: tuple[str | int, ...],
    value: Any,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, consumer = cohort(tmp_path)
    assert all(row.is_start_ready for row in pri.verify_phase1_role_identity([provider, consumer]))
    replace_field(consumer, "source-provenance.json", ("inputs", 0, "origin", "published_dependencies", *keys), value)
    assert pri.verify_s1(consumer).integrity.is_clean

    def forbidden(*_args: object) -> None:
        pytest.fail("invalid or contradictory acquired authority reached provider selection")

    monkeypatch.setattr(pri, "_provider_matches", forbidden)
    result = pri.verify_phase1_role_identity([provider, consumer])[1]
    assert (result.dependencies[0].state, result.dependencies[0].code) == ("mismatch", code)
    assert result.verdict == "BLOCKED" and result.evidence == ()


@pytest.mark.parametrize(
    "fault", ["extra-row", "reordered", "duplicate-ordinal", "invented-dependency", "datasource-kind"]
)
def test_authority_cannot_invent_drop_or_reorder_occurrences(tmp_path: Path, fault: str) -> None:
    provider, consumer = cohort(tmp_path)
    path = provider if fault == "datasource-kind" else consumer
    provenance = json.loads((path / "source-provenance.json").read_bytes())
    if fault == "datasource-kind":
        source = json.loads((consumer / "source-provenance.json").read_bytes())
        provenance["inputs"][0]["origin"]["published_dependencies"] = source["inputs"][0]["origin"][
            "published_dependencies"
        ]
    else:
        authority = provenance["inputs"][0]["origin"]["published_dependencies"]
        spec = json.loads((consumer / "migration-spec.json").read_bytes())
        if fault == "invented-dependency":
            spec["data_sources"] = []
        else:
            authority["rows"].append({**authority["rows"][0], "source_ordinal": 1})
            if fault != "extra-row":
                spec["data_sources"] *= 2
            if fault == "reordered":
                authority["rows"].reverse()
            elif fault == "duplicate-ordinal":
                authority["rows"][1]["source_ordinal"] = 0
        _write(consumer / "migration-spec.json", spec)
    _write(path / "source-provenance.json", provenance)
    reseal(path)
    results = pri.verify_phase1_role_identity([provider, consumer])
    result = results[0 if path == provider else 1]
    code = "published_dependency_authority_" + (
        "invalid" if fault in ("reordered", "duplicate-ordinal") else "contradiction"
    )
    assert code in result.codes()
    assert result.verdict == "BLOCKED"
    assert len(result.dependencies) == (
        0 if fault in ("invented-dependency", "datasource-kind") else 2 if fault != "extra-row" else 1
    )


@pytest.mark.parametrize("published_key", [None, PUBLISHED_KEY.upper(), "other/key"])
def test_sole_acquired_luid_provider_requires_the_exact_key(tmp_path: Path, published_key: str | None) -> None:
    provider, consumer = cohort(tmp_path)
    spec = json.loads((provider / "migration-spec.json").read_bytes())
    if published_key is None:
        del spec["data_sources"][0]["published_datasource"]
    else:
        spec["data_sources"][0]["published_datasource"]["key"] = published_key
    _write(provider / "migration-spec.json", spec)
    reseal(provider)
    result = pri.verify_phase1_role_identity([provider, consumer])[1]
    assert result.dependencies[0].code == "provider_key_contradiction"
    assert result.dependencies[0].provider_ordinal is None


@pytest.mark.parametrize("reverse", [True])
def test_duplicate_acquired_luid_providers_never_choose_input_order(tmp_path: Path, reverse: bool) -> None:
    provider, consumer = cohort(tmp_path)
    duplicate = datasource_package(tmp_path / "duplicate", published_key=PUBLISHED_KEY)
    roots = [provider, duplicate, consumer]
    if reverse:
        roots.reverse()
    result = pri.verify_phase1_role_identity(roots)[roots.index(consumer)]
    assert result.dependencies[0].state == "ambiguous"
    assert result.dependencies[0].code == "provider_ambiguous"
    assert result.dependencies[0].provider_ordinal is None


@pytest.mark.parametrize("resealed", [True])
def test_provenance_change_between_s1_and_held_read_cannot_mix_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    resealed: bool,
) -> None:
    provider, consumer = cohort(tmp_path)
    original = pri.VerifiedPackage.read_verified_member
    changed = []

    def replaced(self, root, name):
        if root == consumer and name == "source-provenance.json" and not changed:
            path = consumer / name
            payload = json.loads(path.read_bytes())
            payload["inputs"][0]["origin"]["published_dependencies"]["rows"][0]["datasource_luid"] = pri.KIND_WORKBOOK
            _write(path, payload)
            if resealed:
                reseal(consumer)
            changed.append(True)
        return original(self, root, name)

    monkeypatch.setattr(pri.VerifiedPackage, "read_verified_member", replaced)
    result = pri.verify_phase1_role_identity([provider, consumer])[1]
    assert changed == [True]
    assert "package_file_digest_mismatch" in result.codes()
    assert result.verdict == "BLOCKED"
    assert all(row.provider_ordinal is None for row in result.dependencies)


def test_provider_model_must_be_its_resolved_declaration(tmp_path: Path) -> None:
    provider, consumer = cohort(tmp_path)
    replace_field(provider, "package-manifest.json", ("artifacts", "model"), None)

    result = pri.verify_phase1_role_identity([provider, consumer])[1]

    assert result.dependencies[0].code == "provider_model_unresolved"
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize("change", ["actual", "summary", "wrong-parent", "false-local-summary"])
def test_consumer_binding_names_the_complete_provider_role(tmp_path: Path, change: str) -> None:
    provider, consumer = cohort(tmp_path)
    assert all(result.is_start_ready for result in pri.verify_phase1_role_identity([provider, consumer]))
    pbir = f"fabric/{WB_UNIT}.Report/definition.pbir"
    if change == "actual":
        replace_field(consumer, pbir, ("datasetReference", "byPath", "path"), "../Wrong.SemanticModel")
    elif change == "summary":
        replace_field(consumer, "package-manifest.json", ("model_binding", "path"), "../Wrong.SemanticModel")
    elif change == "wrong-parent":
        wrong = f"../../../Different/fabric/{DS_UNIT}.SemanticModel"
        replace_field(consumer, pbir, ("datasetReference", "byPath", "path"), wrong)
        replace_field(consumer, "package-manifest.json", ("model_binding", "path"), wrong)
    else:
        replace_field(consumer, "package-manifest.json", ("model_binding", "resolves_in_package"), True)

    result = pri.verify_phase1_role_identity([provider, consumer])[1]

    assert result.dependencies[0].code == "provider_binding_mismatch"
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize(
    "file",
    [
        "migration-spec.json",
        "report.json",
        "engine-output-receipt.json",
        f"handover/{WB_UNIT}.json",
        f"fabric/{WB_UNIT}.Report/definition.pbir",
        "reference/manifest.json",
    ],
)
@pytest.mark.parametrize("tail", ['"extra": 1e999'])
def test_every_identity_json_uses_the_strict_parser(tmp_path: Path, file: str, tail: str) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    add_reference(package)
    path = package / file
    text = path.read_text(encoding="utf-8").rstrip()
    path.write_text(text[:-1] + "," + tail + "}", encoding="utf-8")
    reseal(package)
    assert pri.verify_s1(package).integrity.is_clean

    result = verify_one(package)

    assert "identity_json_invalid" in result.blockers, f"{file} accepted non-strict identity JSON"
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize(
    ("file", "keys", "value"),
    [
        ("source-provenance.json", ("inputs",), [False]),
        ("source-provenance.json", ("inputs", 0, "origin"), True),
        ("source-provenance.json", ("inputs", 0, "origin", "workbook_luid"), "not-a-luid"),
        ("report.json", ("workbooks",), [{"name": WB_UNIT}, None]),
        ("report.json", ("datasources",), {}),
        ("engine-output-receipt.json", ("artifacts",), True),
        ("engine-output-receipt.json", ("artifacts", 0, "path"), []),
        ("migration-spec.json", ("source",), True),
        ("oracle/oracle-manifest.json", ("views",), True),
        ("oracle/oracle-manifest.json", ("views", 0, "image", "bytes"), True),
        ("reference/manifest.json", ("dashboards",), True),
        ("reference/manifest.json", ("dashboards", 0, "states"), {}),
        ("reference/manifest.json", ("dashboards", 0, "states", 0, "sha256"), True),
    ],
)
def test_identity_containers_and_scalars_block_instead_of_crashing(
    tmp_path: Path, file: str, keys: tuple[str | int, ...], value: Any
) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    add_reference(package)
    replace_field(package, file, keys, value)

    result = verify_one(package)

    assert "identity_type_invalid" in result.blockers
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize("unsafe", ["host", "credential", "header"])
def test_complete_packaged_brief_is_checked_for_private_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe: str
) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    token = "s2-synthetic-pat-only"
    monkeypatch.setenv("TABLEAU_PAT_SECRET", token)
    texts = {
        "host": "C:" + "\\" + "Users" + "\\" + "s2-owner" + "\\private.txt",
        "credential": token,
        "header": "X-Tableau-Auth: synthetic-header-token",
    }
    suffix = "A late paragraph: " + texts[unsafe]
    _write(package / "migration-brief.md", brief_text(WB_UNIT, "model_and_report") + suffix)
    reseal(package)

    result = verify_one(package)

    assert role(result, "migration_brief").code == "brief_contains_unsafe_text"
    assert result.verdict == "BLOCKED"
    assert texts[unsafe] not in json.dumps(result.as_dict())
