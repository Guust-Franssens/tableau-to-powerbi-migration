"""Direct controls for PR 600's ten S2 review reproductions; no producer-certified fixtures."""

# Tests name their cases; exact empty-container assertions also guard the returned shape.
# pylint: disable=missing-function-docstring,use-implicit-booleaness-not-comparison

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

from test_package_filesystem import link_directory
from test_package_role_identity import (
    DS_LUID,
    DS_UNIT,
    PUBLISHED_KEY,
    WB_UNIT,
    _write,
    brief_text,
    datasource_package,
    pri,
    role,
    seal,
    verify_one,
    workbook_package,
)


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
        published={"luid": provider_luid, "key": PUBLISHED_KEY},
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
@pytest.mark.parametrize(
    "spelling",
    [
        "/outside-secret.png",
        "C:/outside-secret.png",
        r"dashboard\view.png",
        "./dashboard/view.png",
        "../outside-secret.png",
        "dashboard/../dashboard/view.png",
        "dashboard//view.png",
        "DASHBOARD/view.png",
        "dashboard/view.png.",
        "dashboard/missing.png",
    ],
)
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


@pytest.mark.parametrize("field", ["path", "retained_path"])
@pytest.mark.parametrize("leg", ["image", "svg", "pdf", "data"])
def test_every_declared_oracle_leg_path_is_checked(tmp_path: Path, field: str, leg: str) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    replace_field(
        package, "oracle/oracle-manifest.json", ("views", 0, leg), {"status": "failed", field: "../outside.csv"}
    )

    result = verify_one(package)

    assert role(result, "tableau_oracle").code == "evidence_path_not_verified"
    assert result.verdict == "BLOCKED"


def test_resolved_evidence_carries_the_walk_produced_path_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    add_reference(package)
    walked = {}
    original = pri.pfs.walk_package

    def tracked(root: Path):
        files, findings, empty = original(root)
        if not walked:
            walked.update(files)
        return files, findings, empty

    monkeypatch.setattr(pri.pfs, "walk_package", tracked)
    result = verify_one(package)

    assert result.verdict == "START_READY", result.blockers
    assert len(result.evidence) == 2
    namespace = {member.relative_path: member.path for member in result.verified.integrity.verified_files}
    for evidence in result.evidence:
        key = f"{evidence.origin}/dashboard/view.png"
        assert evidence.render_path is namespace[key] is walked[key]


@pytest.mark.parametrize(
    "published",
    [
        {},
        {"id": DS_UNIT},
        True,
        [],
        "caption",
        {"luid": True},
        {"key": []},
        None,
        False,
        0,
        0.5,
        "",
        {"luid": "not-a-luid"},
    ],
)
def test_invalid_published_rows_remain_counted_and_block(tmp_path: Path, published: Any) -> None:
    provider, consumer = cohort(tmp_path)
    spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
    spec["data_sources"].append({"published_datasource": published})
    _write(consumer / "migration-spec.json", spec)
    reseal(consumer)

    result = pri.verify_phase1_role_identity([provider, consumer])[1]

    assert len(result.dependencies) == 2, "a declared row disappeared from the dependency denominator"
    assert result.dependencies[0].state == "resolved"
    expected = (
        "published_dependency_identity_missing"
        if published in ({}, {"id": DS_UNIT})
        else "published_dependency_invalid"
    )
    assert (result.dependencies[1].state, result.dependencies[1].code) == ("mismatch", expected)
    assert result.topology == "published_consumer"
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize("value", [True, {}, "rows", [None], [False], None, False, 0, 0.5, "", [0], ["row"], [[]]])
def test_malformed_datasource_collections_are_not_owned_model_fallbacks(tmp_path: Path, value: Any) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    replace_field(package, "migration-spec.json", ("data_sources",), value)

    result = verify_one(package)

    assert len(result.dependencies) == 1
    assert result.dependencies[0].code == "published_dependency_invalid"
    assert result.topology == "published_consumer"
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize("kind", ["workbook", "datasource"])
def test_missing_datasource_collection_is_invalid(tmp_path: Path, kind: str) -> None:
    """Missing topology cannot mean either owned-model readiness or datasource-only earned N/A."""
    package = workbook_package(tmp_path / WB_UNIT) if kind == "workbook" else datasource_package(tmp_path / DS_UNIT)
    assert verify_one(package).verdict == "START_READY"
    spec = json.loads((package / "migration-spec.json").read_text(encoding="utf-8"))
    del spec["data_sources"]
    _write(package / "migration-spec.json", spec)
    reseal(package)
    assert pri.verify_s1(package).integrity.is_clean

    result = verify_one(package)

    assert "published_dependency_invalid" in result.blockers
    assert result.verdict == "BLOCKED"
    if kind == "workbook":
        assert len(result.dependencies) == 1
        assert (result.dependencies[0].state, result.dependencies[0].code) == (
            "mismatch",
            "published_dependency_invalid",
        )
        assert result.topology == "published_consumer"


def test_null_published_datasource_is_not_an_absent_dependency(tmp_path: Path) -> None:
    """Only absence of the optional key means the row is non-published."""
    package = workbook_package(tmp_path / WB_UNIT)
    row = {"id": "ds-1", "connection": {"class": "textscan", "mode": "live"}, "fields": []}
    replace_field(package, "migration-spec.json", ("data_sources",), [row])
    absent = verify_one(package)
    assert absent.verdict == "START_READY"
    assert absent.topology == "owned_model"
    assert absent.dependencies == ()

    replace_field(package, "migration-spec.json", ("data_sources", 0, "published_datasource"), None)
    assert pri.verify_s1(package).integrity.is_clean
    present = verify_one(package)

    assert len(present.dependencies) == 1, "an explicitly null dependency disappeared from the denominator"
    assert (present.dependencies[0].state, present.dependencies[0].code) == (
        "mismatch",
        "published_dependency_invalid",
    )
    assert present.topology == "published_consumer"
    assert present.verdict == "BLOCKED"


@pytest.mark.parametrize("source_count", [0, 1, 2])
def test_valid_non_published_source_collections_keep_owned_model_topology(tmp_path: Path, source_count: int) -> None:
    """The real spec schema permits an explicit empty list, not an omitted collection."""
    package = workbook_package(tmp_path / WB_UNIT)
    spec = json.loads((package / "migration-spec.json").read_text(encoding="utf-8"))
    spec.update(
        migration_spec_version="1.0",
        worksheets=[],
        dashboards=[],
        data_sources=[
            {"id": f"ds-{index}", "connection": {"class": "textscan", "mode": "live"}, "fields": []}
            for index in range(source_count)
        ],
    )
    schema_path = Path(__file__).resolve().parents[1] / "docs" / "migration-spec.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert "data_sources" in schema["required"]
    Draft7Validator(schema).validate(spec)
    _write(package / "migration-spec.json", spec)
    reseal(package)

    result = verify_one(package)

    assert result.verdict == "START_READY", result.blockers
    assert result.topology == "owned_model"
    assert result.dependencies == ()
    assert role(result, "fabric_model").state == "resolved"


@pytest.mark.parametrize("provider_luid", [DS_LUID, None], ids=["luid", "published-key"])
def test_non_published_rows_do_not_hide_a_valid_published_dependency(tmp_path: Path, provider_luid: str | None) -> None:
    """A legitimate non-published row adds no edge; the valid published row still resolves."""
    provider, consumer = cohort(tmp_path, provider_luid=provider_luid)
    spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
    local = {"id": "local", "connection": {"class": "textscan", "mode": "live"}, "fields": []}
    spec["data_sources"] = [local, *spec["data_sources"], local]
    _write(consumer / "migration-spec.json", spec)
    reseal(consumer)

    results = pri.verify_phase1_role_identity([provider, consumer])

    assert len(results) == 2
    result = results[1]
    assert results[0].verdict == result.verdict == "START_READY"
    assert result.topology == "published_consumer"
    assert len(result.dependencies) == 1
    assert result.dependencies[0].state == "resolved"
    assert result.dependencies[0].datasource_luid == provider_luid
    assert result.dependencies[0].published_key == PUBLISHED_KEY


def test_duplicate_published_rows_do_not_disappear(tmp_path: Path) -> None:
    provider, consumer = cohort(tmp_path)
    spec = json.loads((consumer / "migration-spec.json").read_text(encoding="utf-8"))
    spec["data_sources"] *= 2
    _write(consumer / "migration-spec.json", spec)
    reseal(consumer)

    result = pri.verify_phase1_role_identity([provider, consumer])[1]

    assert result.verdict == "START_READY", result.blockers
    assert len(result.dependencies) == 2, "every declared row must have a result, even identical rows"


def test_matching_luid_cannot_override_a_conflicting_published_key(tmp_path: Path) -> None:
    provider, consumer = cohort(tmp_path)
    replace_field(
        consumer, "migration-spec.json", ("data_sources", 0, "published_datasource", "key"), "other-site/other-source"
    )

    result = pri.verify_phase1_role_identity([provider, consumer])[1]

    assert result.dependencies[0].code == "provider_key_contradiction"
    assert result.verdict == "BLOCKED"


def test_key_fallback_cannot_use_a_provider_with_an_established_luid(tmp_path: Path) -> None:
    provider, consumer = cohort(tmp_path)
    replace_field(consumer, "migration-spec.json", ("data_sources", 0, "published_datasource", "luid"), None)

    result = pri.verify_phase1_role_identity([provider, consumer])[1]

    assert result.dependencies[0].code == "provider_luid_contradiction"
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize("blocked_first", [False, True])
def test_a_consumer_cannot_use_a_provider_with_a_blocked_local_role(tmp_path: Path, blocked_first: bool) -> None:
    provider, consumer = cohort(tmp_path)
    _write(provider / "migration-brief.md", brief_text("WrongUnit", "model_only"))
    reseal(provider)
    roots = [provider, consumer] if blocked_first else [consumer, provider]

    results = pri.verify_phase1_role_identity(roots)
    consumer_result = results[1 if blocked_first else 0]
    provider_result = results[0 if blocked_first else 1]

    assert role(provider_result, "migration_brief").code == "brief_unit_mismatch"
    assert consumer_result.dependencies[0].code == "provider_not_s2_clean"
    assert consumer_result.verdict == "BLOCKED"


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


def test_binding_normalisation_uses_the_declared_model_not_the_unit_name(tmp_path: Path) -> None:
    provider, consumer = cohort(tmp_path)
    (provider / "fabric" / f"{DS_UNIT}.SemanticModel").rename(provider / "fabric" / "Actual.SemanticModel")
    replace_field(provider, "package-manifest.json", ("artifacts", "model"), "fabric/Actual.SemanticModel")
    receipt = json.loads((provider / "engine-output-receipt.json").read_text(encoding="utf-8"))
    for row in receipt["artifacts"]:
        row["path"] = row["path"].replace(f"{DS_UNIT}.SemanticModel", "Actual.SemanticModel")
    _write(provider / "engine-output-receipt.json", receipt)
    reseal(provider)
    binding = f"../../../{DS_UNIT}/fabric/./Actual.SemanticModel"
    replace_field(consumer, f"fabric/{WB_UNIT}.Report/definition.pbir", ("datasetReference", "byPath", "path"), binding)
    replace_field(consumer, "package-manifest.json", ("model_binding", "path"), binding)

    result = pri.verify_phase1_role_identity([provider, consumer])[1]

    assert result.verdict == "START_READY", result.blockers
    assert result.dependencies[0].model_role == "fabric/Actual.SemanticModel"


@pytest.mark.parametrize(
    "file",
    [
        "migration-spec.json",
        "migration-spec.schema.json",
        "source-provenance.json",
        "report.json",
        "engine-output-receipt.json",
        f"handover/{WB_UNIT}.json",
        f"fabric/{WB_UNIT}.Report/definition.pbir",
        "oracle/oracle-manifest.json",
        "reference/manifest.json",
    ],
)
@pytest.mark.parametrize("tail", ['"extra": 0, "extra": 1', '"extra": NaN', '"extra": 1e999'])
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
        ("source-provenance.json", ("inputs",), True),
        ("source-provenance.json", ("inputs",), {}),
        ("source-provenance.json", ("inputs",), [False]),
        ("source-provenance.json", ("inputs", 0, "input"), []),
        ("source-provenance.json", ("inputs", 0, "origin"), True),
        ("source-provenance.json", ("inputs", 0, "origin", "workbook_luid"), {}),
        ("source-provenance.json", ("inputs", 0, "origin", "workbook_luid"), "not-a-luid"),
        ("report.json", ("workbooks",), True),
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


@pytest.mark.parametrize(
    ("file", "key", "expected_role"),
    [
        ("source-provenance.json", "inputs", "source_provenance"),
        ("engine-output-receipt.json", "artifacts", "engine_receipt"),
        ("report.json", "workbooks", "engine_classification"),
        ("oracle/oracle-manifest.json", "views", "tableau_oracle"),
        ("reference/manifest.json", "dashboards", "tableau_reference"),
    ],
)
def test_malformed_extra_rows_are_not_filtered_out(tmp_path: Path, file: str, key: str, expected_role: str) -> None:
    package = workbook_package(tmp_path / WB_UNIT)
    add_reference(package)
    payload = json.loads((package / file).read_text(encoding="utf-8"))
    payload[key].append(False)
    _write(package / file, payload)
    reseal(package)

    result = verify_one(package)

    assert role(result, expected_role).code == "identity_type_invalid"
    assert result.verdict == "BLOCKED"


@pytest.mark.parametrize("origin", ["handover", "reference", "oracle", "_oracle"])
@pytest.mark.parametrize("form", ["declared", "walked"])
def test_datasource_not_applicable_requires_absent_foreign_roles(tmp_path: Path, origin: str, form: str) -> None:
    package = datasource_package(tmp_path / DS_UNIT)
    assert verify_one(package).verdict == "START_READY"
    artifact = "oracle" if origin == "_oracle" else origin
    expected_role = "handover" if artifact == "handover" else f"tableau_{artifact}"
    if form == "walked":
        _write(package / origin / "foreign.json", {"foreign": True})
        reseal(package)
    else:
        replace_field(package, "package-manifest.json", ("artifacts", artifact), origin)

    result = verify_one(package)

    assert (role(result, expected_role).state, role(result, expected_role).code) == (
        "mismatch",
        "inapplicable_role_present",
    )
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
