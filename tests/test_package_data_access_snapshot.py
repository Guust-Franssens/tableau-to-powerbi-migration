"""Direct controls for PR #608's producer snapshots, final reseal and published-only inheritance."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree

import pytest

import test_data_access_contract as authority
import test_package_role_identity as s2
import test_package_unit_reproductions as producer
from test_data_access_contract import _root_fixture  # noqa: F401  # shared pytest fixture
from test_package_unit_gates import DS_LUID, DS_UNIT, UNIT, _brief, _bundle, pkg


def _files(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _local_bundle(parent: Path) -> tuple[Path, Path, dict]:
    bundle, oracle, _objects = _bundle(parent, covered=None)
    source = parent / "rows.csv"
    source.write_bytes(b"value\n7\n")
    tables = bundle / "pbip" / UNIT / f"{UNIT}.SemanticModel" / "definition" / "tables"
    tables.mkdir()
    (tables / "Rows.tmdl").write_text(
        f'table Rows\n\tpartition Rows = m\n\t\tmode: import\n\t\tsource = Csv.Document(File.Contents("{source}"))\n',
        encoding="utf-8",
    )
    return (
        bundle,
        parent / "out",
        {
            "oracle_dir": oracle,
            "assets_dir": bundle.parent / "assets",
            "brief": _brief(parent, UNIT),
        },
    )


def _binding_package(parent: Path, rows: bytes = b"value\n7\n") -> Path:
    """Real producer output with accepted local authority and an eligible folder declaration."""
    bundle, out, options = _local_bundle(parent)
    (parent / "rows.csv").write_bytes(rows)
    pkg.package_unit(bundle, UNIT, out, **options)
    root = out / UNIT
    assert pkg.pri.verify_s1(root).integrity.is_clean
    assert pkg.pri.verify_phase1_role_identity((root,))[0].is_start_ready
    assert pkg.data_access.read_data_access(root / "data-access.json").state == "local_import_ready"
    return root


def test_binding_roundtrip_keeps_exact_bytes_and_authority(tmp_path: Path) -> None:
    """Independent old/new literal and digest expectations, through the three public APIs."""
    root = _binding_package(tmp_path)
    before = _files(root)
    expression = f"fabric/{UNIT}.SemanticModel/definition/expressions.tmdl"
    original_manifest = json.loads(before["package-manifest.json"])
    result = pkg.bind_package(root)
    assert (result.exit_code, result.outcome, result.code) == (0, "published", "binding_bound")
    after = _files(root)
    assert after[expression] == before[expression].replace(b"<PACKAGE_ROOT>", str(root).encode("utf-8"))
    allowed = {expression, "README.md", "handover.md", "package-manifest.json"}
    assert set(before) == set(after)
    assert all(before[key] == raw for key, raw in after.items() if key not in allowed)
    manifest = json.loads(after["package-manifest.json"])
    for key, raw in after.items():
        if key != "package-manifest.json":
            assert manifest["contents"]["files"][key] == hashlib.sha256(raw).hexdigest()
    for key in original_manifest.keys() - {"data_sources", "notes", "contents"}:
        assert manifest[key] == original_manifest[key], f"unrelated manifest field changed: {key}"
    assert {key: value for key, value in manifest["data_sources"].items() if key != "binding"} == {
        key: value for key, value in original_manifest["data_sources"].items() if key != "binding"
    }
    inspected = pkg.inspect_package(root)
    assert inspected.exit_code == 0 and inspected.parameters
    assert all(row.matches_current_root and row.target_exists for row in inspected.parameters)
    assert str(root) not in json.dumps(inspected.as_dict()) + repr(inspected)
    assert "START_READY" not in json.dumps(result.as_dict())
    assert _files(root) == after, "inspection is read-only"
    repeated = pkg.bind_package(root)
    assert (repeated.exit_code, repeated.outcome) == (0, "unchanged")
    assert _files(root) == after, "an idempotent bind does not reserialize anything"
    sanitized = pkg.sanitize_package(root)
    assert (sanitized.exit_code, sanitized.outcome, sanitized.code) == (0, "published", "binding_unbound")
    assert _files(root) == before, "sanitize must restore the exact portable bytes, not render/rebaseline"
    clean = pkg.inspect_package(root)
    assert clean.exit_code == 0 and all(row.code == "binding_placeholder" for row in clean.parameters)
    assert pkg.pri.verify_s1(root).integrity.is_clean


def _binding_provider(parent: Path, rows: bytes = b"value\n19\n", *, gate_root: Path | None = None) -> Path:
    bundle, oracle, _objects = _bundle(parent, covered=None, datasource_only=True)
    asset = parent / "assets" / f"{DS_LUID}_{DS_UNIT}.tds"
    document = ElementTree.parse(asset)
    connection = document.getroot().find("connection")
    assert connection is not None
    connection.attrib.clear()
    connection.attrib.update(
        {"class": "sqlserver", "server": "source.example", "dbname": "db"}
        if gate_root is not None
        else {"class": "textscan", "filename": "provider.csv"}
    )
    document.write(asset, encoding="utf-8", xml_declaration=True)
    manifest_path = bundle / "input_manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    next(row for row in manifest["assets"] if row["name"] == asset.name)["sha256"] = hashlib.sha256(
        asset.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    source = parent / "provider.csv"
    source.write_bytes(rows)
    tables = bundle / "pbip" / DS_UNIT / f"{DS_UNIT}.SemanticModel" / "definition" / "tables"
    tables.mkdir()
    (tables / "Rows.tmdl").write_text(
        f'table Rows\n\tpartition Rows = m\n\t\tmode: import\n\t\tsource = Csv.Document(File.Contents("{source}"))\n',
        encoding="utf-8",
    )
    out = parent / "out"
    pkg.package_unit(
        bundle,
        DS_UNIT,
        out,
        oracle_dir=oracle,
        assets_dir=parent / "assets",
        brief=_brief(parent, DS_UNIT, "model_only", "model_only_unvalidated" if gate_root is not None else "stop"),
        gate_root=gate_root,
    )
    root = out / DS_UNIT
    assert pkg.data_access.read_data_access(root / "data-access.json").state == (
        "authorized_model_only" if gate_root is not None else "local_import_ready"
    )
    return root


def _binding_consumer(parent: Path, provider: Path) -> Path:
    root = parent / "Consumer"
    binding = os.path.relpath(
        provider / "fabric" / f"{DS_UNIT}.SemanticModel", root / "fabric" / "Revenue.Report"
    ).replace("\\", "/")
    s2.workbook_package(root, published={"luid": DS_LUID}, binding=binding)
    manifest = json.loads((root / "package-manifest.json").read_bytes())
    manifest["data_sources"] = copy.deepcopy(authority.LOCAL)
    (root / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    producer._projection_fixture(
        root,
        {
            **producer.LOCAL_PROJECTION,
            "state": "provider_inherited",
            "provider_state": "local_import_ready",
            "provider_unit": pkg.data_access.provider_reference(DS_UNIT),
            "effective_scope": "report_only_shared_model",
            "codes": ["provider-exact"],
        },
    )
    return root


@pytest.mark.parametrize("operation", [pkg.bind_package, pkg.inspect_package, pkg.sanitize_package])
@pytest.mark.parametrize("damage", ["legacy-binding", "spec", "source", "oracle", "data", "projection"])
def test_binding_refuses_dirty_baselines_without_legitimizing_them(tmp_path: Path, operation, damage: str) -> None:
    root = _binding_package(tmp_path)
    manifest = json.loads((root / "package-manifest.json").read_bytes())
    targets = {
        "spec": "migration-spec.json",
        "source": manifest["artifacts"]["asset"],
        "oracle": next(
            key for key in manifest["contents"]["files"] if key.startswith("oracle/") and key.endswith(".csv")
        ),
        "data": manifest["data_sources"]["shipped"][0]["path"],
        "projection": "data-access.json",
        "legacy-binding": f"fabric/{UNIT}.SemanticModel/definition/expressions.tmdl",
    }
    path = root / targets[damage]
    if damage == "legacy-binding":
        path.write_bytes(path.read_bytes().replace(b"<PACKAGE_ROOT>", str(root).encode("utf-8")))
    else:
        path.unlink()
    before = _files(root)
    identity = root.lstat().st_ino
    result = operation(root)
    assert (result.exit_code, result.code, result.outcome) == (1, "binding_s1_dirty", "unchanged")
    assert _files(root) == before and root.lstat().st_ino == identity
    assert not pkg.staging_dir(root.parent, root.name).exists()
    assert not pkg.retired_dir(root).exists()


@pytest.mark.parametrize("operation", [pkg.bind_package, pkg.inspect_package, pkg.sanitize_package])
@pytest.mark.parametrize("member", ["README.md", "handover.md", "notes"])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_binding_requires_exact_owned_prose_even_with_a_clean_s1(
    tmp_path: Path, operation, member: str, ambiguous: bool
) -> None:
    root = _binding_package(tmp_path)
    if member == "notes":
        manifest = json.loads((root / "package-manifest.json").read_bytes())
        note = next(note for note in manifest["notes"] if note.startswith("this package is UNBOUND:"))
        manifest["notes"].append(note) if ambiguous else manifest["notes"].remove(note)
        (root / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        path = root / member
        raw = path.read_bytes()
        path.write_bytes(
            raw + raw
            if ambiguous
            else raw.replace(b"binding is a step", b"binding is not a step").replace(
                b"this package is UNBOUND:",
                b"custom binding reminder:",
            )
        )
    producer._reseal(root)
    assert pkg.pri.verify_s1(root).integrity.is_clean
    before = _files(root)
    result = operation(root)
    assert (result.exit_code, result.code) == (3, "binding_prose_unestablished")
    assert _files(root) == before
    assert not pkg.staging_dir(root.parent, root.name).exists()


@pytest.mark.parametrize("change", ["disabled-rewrite", "unrelated-expression", "omitted-digest", "manifest-field"])
def test_binding_exact_delta_rejects_real_planner_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    import set_data_folder as binder  # pylint: disable=import-outside-toplevel

    root = _binding_package(tmp_path)
    before = _files(root)
    hits = []
    rewrite, plan = binder._rewritten, pkg._plan_package_binding

    def mutated_rewrite(text: str, base: str) -> tuple[str, int, list[str]]:
        changed, count, untouched = rewrite(text, base)
        assert changed != text, "the control must contain a real eligible placeholder"
        hits.append(change)
        return (
            (text if change == "disabled-rewrite" else changed + "\nannotation Unexpected = true\n"),
            count,
            untouched,
        )

    def mutated_plan(*args, **kwargs):
        generated = plan(*args, **kwargs)
        payload = json.loads(generated["package-manifest.json"])
        if change == "omitted-digest":
            expression = next(key for key in generated if key.endswith("expressions.tmdl"))
            payload["contents"]["files"][expression] = json.loads(before["package-manifest.json"])["contents"]["files"][
                expression
            ]
        else:
            payload["engine"] = "unrelated engine mutation"
        generated["package-manifest.json"] = json.dumps(payload).encode("utf-8")
        hits.append(change)
        return generated

    if change in ("disabled-rewrite", "unrelated-expression"):
        result = pkg.bind_package(root, planner=mutated_rewrite)
        expected = "binding_delta_refused"
    else:
        monkeypatch.setattr(pkg, "_plan_package_binding", mutated_plan)
        result = pkg.bind_package(root)
        expected = "binding_seal_refused"
    assert hits == [change]
    assert (result.exit_code, result.code) == (1, expected), "the exact-delta authority must reject the intended input"
    assert _files(root) == before and not pkg.staging_dir(root.parent, root.name).exists()


def test_binding_missing_shipped_data_is_not_fixed_by_resealing(tmp_path: Path) -> None:
    root = _binding_package(tmp_path)
    manifest = json.loads((root / "package-manifest.json").read_bytes())
    (root / manifest["data_sources"]["shipped"][0]["path"]).unlink()
    producer._reseal(root)
    assert pkg.pri.verify_s1(root).integrity.is_clean
    before = _files(root)
    result = pkg.bind_package(root)
    assert (result.exit_code, result.code) == (1, "binding_shipped_data_missing")
    assert _files(root) == before


@pytest.mark.parametrize("state", ["blocked", "cannot_establish", "missing", "malformed"])
def test_binding_never_upgrades_unaccepted_projection(tmp_path: Path, state: str) -> None:
    root = _binding_package(tmp_path)
    path = root / "data-access.json"
    if state == "missing":
        path.unlink()
    elif state == "malformed":
        path.write_bytes(b'{"state":"live_data_ok"}')
    else:
        payload = {
            **producer.LOCAL_PROJECTION,
            "state": state,
            "validation": "not_established",
            "effective_scope": None,
            "max_phase2_claim": "none",
            "codes": ["marker-only" if state == "blocked" else "audit-missing"],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
    producer._reseal(root)
    before = _files(root)
    result = pkg.bind_package(root)
    assert result.exit_code == (1 if state == "blocked" else 3)
    assert result.code in {"binding_projection_not_accepted", "binding_projection_unestablished"}
    assert _files(root) == before


def test_binding_preserves_authentic_model_only_authorization(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An actual gate authorization produces the projection; binding never accesses that gate."""
    authority._authorize(root)
    authorized = authority._assess(root, authorized=True)
    assert (authorized.state, authorized.validation) == ("authorized_model_only", "unvalidated")
    package = _binding_provider(tmp_path / "package-source", gate_root=root)
    prior = _files(package)

    def never_assess(*_args, **_kwargs):
        pytest.fail("binding attempted to read/upgrade original gate authority")

    for name in ("assess_data_access", "_audit_entries", "_read_audit_trail", "authorize", "clear_block", "_audit"):
        monkeypatch.setattr(pkg.data_access, name, never_assess)
    result = pkg.bind_package(package)
    assert (result.exit_code, result.code) == (0, "binding_bound")
    assert (package / "data-access.json").read_bytes() == prior["data-access.json"]
    assert pkg.data_access.read_data_access(package / "data-access.json") == authorized
    assert "unvalidated" not in json.dumps(result.as_dict()).lower(), "binding must not add its own validation state"


@pytest.mark.parametrize("providers", ["unbound", "missing", "duplicate", "foreign", "accepted"])
def test_binding_consumer_uses_read_only_explicit_providers(tmp_path: Path, providers: str) -> None:
    provider = _binding_provider(tmp_path / "provider")
    consumer = _binding_consumer(tmp_path / "consumer", provider)
    if providers != "unbound":
        assert pkg.bind_package(provider).exit_code == 0
        assert pkg.inspect_package(provider).code == "binding_bound"
    cohort = () if providers == "missing" else (provider, provider) if providers == "duplicate" else (provider,)
    if providers == "foreign":
        other = _binding_package(tmp_path / "other")
        assert pkg.bind_package(other).exit_code == 0
        cohort = (other,)
    before, provider_bytes = _files(consumer), _files(provider)
    result = pkg.bind_package(consumer, provider_packages=cohort)
    if providers == "accepted":
        assert (result.exit_code, result.code) == (0, "binding_not_applicable")
        assert pkg.inspect_package(consumer, provider_packages=cohort).exit_code == 0
        assert pkg.sanitize_package(consumer).exit_code == 0, "sanitize never needs provider arguments"
    else:
        assert result.exit_code == 1
        assert result.code in {"binding_provider_not_bound", "binding_roles_refused"}
    assert _files(consumer) == before and _files(provider) == provider_bytes


def test_binding_provider_mutation_after_s2_is_caught_by_held_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _binding_provider(tmp_path / "provider")
    assert pkg.bind_package(provider).exit_code == 0
    consumer = _binding_consumer(tmp_path / "consumer", provider)
    original = _files(consumer)
    verify = pkg.pri.verify_phase1_role_identity
    hits = []

    def mutate(roots, **kwargs):
        result = verify(roots, **kwargs)
        if not hits:
            path = next((provider / "data").rglob("*.csv"))
            path.write_bytes(b"value\nchanged-provider-row\n")
            producer._reseal(provider)
            assert pkg.pri.verify_s1(provider).integrity.is_clean
            hits.append(True)
        return result

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", mutate)
    result = pkg.bind_package(consumer, provider_packages=(provider,))
    assert hits == [True]
    assert (result.exit_code, result.code) == (3, "binding_authority_changed")
    assert _files(consumer) == original
    assert b"changed-provider-row" in next((provider / "data").rglob("*.csv")).read_bytes()


@pytest.mark.parametrize("wrong_ordinal", [False, True])
def test_binding_holds_s2_provider_ordinals_in_the_exact_ordered_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_ordinal: bool
) -> None:
    selected = _binding_provider(tmp_path / "selected")
    assert pkg.bind_package(selected).exit_code == 0
    other = producer._direct_provider(tmp_path / "other", luid=s2.WB_LUID)
    manifest = json.loads((other / "package-manifest.json").read_bytes())
    manifest["data_sources"] = copy.deepcopy(authority.LOCAL)
    (other / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    producer._reseal(other)
    assert pkg.bind_package(other).code == "binding_not_applicable"
    consumer = _binding_consumer(tmp_path / "consumer", selected)
    providers = (other, selected)
    verify = pkg.pri.verify_phase1_role_identity
    calls, hits = [], []

    def ordinal_at(roots, **kwargs):
        assert tuple(roots) == (*providers, consumer)
        result = verify(roots, **kwargs)
        assert result[-1].dependencies[0].provider_ordinal == 1
        calls.append(tuple(roots))
        if wrong_ordinal and len(calls) == 2:
            dependency = replace(result[-1].dependencies[0], provider_ordinal=0)
            result = (*result[:-1], replace(result[-1], dependencies=(dependency,)))
            hits.append("ordinal")
        return result

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", ordinal_at)
    before = tuple(_files(root) for root in (*providers, consumer))
    result = pkg.bind_package(consumer, provider_packages=providers)
    assert len(calls) == 2, "idempotent/N/A operations also require a final fixed-cohort recheck"
    assert (result.exit_code, result.code) == (
        (3, "binding_cohort_changed") if wrong_ordinal else (0, "binding_not_applicable")
    )
    assert hits == (["ordinal"] if wrong_ordinal else [])
    assert tuple(_files(root) for root in (*providers, consumer)) == before


def test_binding_datasource_applicability_requires_actual_declaration_inspection(tmp_path: Path) -> None:
    root = _binding_provider(tmp_path)
    inspected = pkg.inspect_package(root)
    assert inspected.applicable is True and inspected.code == "binding_unbound"
    expression = next(root.glob("fabric/*.SemanticModel/definition/expressions.tmdl"))
    expression.write_bytes(b"expression NotAFolder = 7\n")
    producer._reseal(root)
    assert pkg.pri.verify_s1(root).integrity.is_clean
    result = pkg.bind_package(root)
    assert (result.exit_code, result.code) == (3, "binding_declaration_missing")


def test_binding_inspection_preserves_parameter_ordinals_data_tails_and_separator_intent(tmp_path: Path) -> None:
    root = _binding_package(tmp_path)
    expression = next(root.glob("fabric/*.SemanticModel/definition/expressions.tmdl"))
    nested = root / "data" / "Nested.Data"
    nested.mkdir()
    (nested / "other.csv").write_bytes(b"value\n23\n")
    separator = os.sep
    expression.write_bytes(
        expression.read_bytes()
        + (
            'expression Label = "ordinary value"\n'
            f'expression SourceFolder = "<PACKAGE_ROOT>{separator}data{separator}Nested.Data"\n'
        ).encode("utf-8")
    )
    producer._reseal(root)
    before = expression.read_bytes()
    result = pkg.bind_package(root)
    assert result.exit_code == 0
    assert [(row.ordinal, row.parameter, row.data_tail, row.trailing_separator) for row in result.parameters] == [
        (0, "DataFolder", "", True),
        (2, "SourceFolder", "Nested.Data", False),
    ]
    assert all(row.matches_current_root and row.target_exists for row in result.parameters)
    assert expression.read_bytes() == before.replace(b"<PACKAGE_ROOT>", str(root).encode("utf-8"))
    assert pkg.sanitize_package(root).exit_code == 0
    assert expression.read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        "csv-delete",
        "csv-replace",
        "spec",
        "brief",
        "source",
        "model",
        "report",
        "provenance",
        "handover-json",
        "unexpected",
        "manifest",
        "projection",
    ],
)
def test_post_s2_candidate_mutation_refuses_without_rebaselining(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    bundle, out, options = _local_bundle(tmp_path)
    pkg.package_unit(bundle, UNIT, out, **options)
    prior = _files(out / UNIT)
    verify = pkg.pri.verify_phase1_role_identity
    attacked = []

    def mutate_after_s2(roots, **kwargs):
        results = verify(roots, **kwargs)
        if not attacked:
            candidate = roots[-1]
            assert results[-1].is_start_ready, results[-1].codes()
            manifest = json.loads((candidate / "package-manifest.json").read_text(encoding="utf-8"))
            targets = {
                "csv-delete": manifest["data_sources"]["shipped"][0]["path"],
                "csv-replace": manifest["data_sources"]["shipped"][0]["path"],
                "spec": "migration-spec.json",
                "brief": "migration-brief.md",
                "source": manifest["artifacts"]["asset"],
                "model": f"fabric/{UNIT}.SemanticModel/definition/model.tmdl",
                "report": f"fabric/{UNIT}.Report/definition.pbir",
                "provenance": "source-provenance.json",
                "handover-json": f"handover/{UNIT}.json",
                "unexpected": "unexpected.txt",
                "manifest": "package-manifest.json",
                "projection": "data-access.json",
            }
            path = candidate / targets[change]
            if change == "csv-delete":
                path.unlink()
            elif change == "spec":
                spec = json.loads(path.read_text(encoding="utf-8"))
                spec["data_sources"] = authority._spec(authority.LIVE)["data_sources"]
                path.write_text(json.dumps(spec), encoding="utf-8")
            else:
                path.write_bytes((path.read_bytes() if path.exists() else b"") + b"\nchanged-after-S2\n")
            attacked.append(change)
        return results

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", mutate_after_s2)
    with pytest.raises(pkg.PackagingError, match="^data_access_candidate_changed$"):
        pkg.package_unit(bundle, UNIT, out, **options)
    assert attacked == [change], "the intended post-S2 seam must actually run"
    assert _files(out / UNIT) == prior
    assert not pkg.staging_dir(out, UNIT).exists()


def test_live_spec_cannot_be_replaced_by_flat_rows_after_s2(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = producer._assessment_candidate(tmp_path, authority._spec(authority.LIVE))
    verify = pkg.pri.verify_phase1_role_identity
    calls = []

    def replace_spec(roots, **kwargs):
        result = verify(roots, **kwargs)
        spec = json.loads((candidate / "migration-spec.json").read_text(encoding="utf-8"))
        assert spec["data_sources"][0]["connection"] == authority.LIVE
        spec["data_sources"] = authority._spec(authority.FLAT)["data_sources"]
        s2._write(candidate / "migration-spec.json", spec)
        producer._reseal(candidate)
        assert pkg.pri.verify_s1(candidate).integrity.is_clean, "replacing AND resealing must not help"
        calls.append(True)
        return result

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", replace_spec)
    result = producer._assess_candidate(candidate, root)
    assert calls == [True]
    assert (result.state, result.codes) == ("cannot_establish", ("projection-invalid",))


def test_assessment_receives_pre_s2_spec_policy_and_localization_copies(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = producer._assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    local = copy.deepcopy(authority.LOCAL)
    inputs = pkg._data_access_inputs(candidate, local, ())
    held_spec = copy.deepcopy(inputs.spec)
    local["self_contained"] = False
    observations = []
    assess = pkg.data_access.assess_data_access

    def held_facts(gate_root, **kwargs):
        assert kwargs["package_spec"] == held_spec
        assert kwargs["package_spec"] is not inputs.spec
        assert kwargs["package_data_sources"] == authority.LOCAL
        assert kwargs["package_data_sources"] is not inputs.localized
        assert kwargs["fallback_authorization"] == "stop"
        assert kwargs["requested_scope"] == "model_and_report"
        observations.append(True)
        return assess(gate_root, **kwargs)

    monkeypatch.setattr(pkg.data_access, "assess_data_access", held_facts)
    read_text = Path.read_text

    def no_second_spec_read(path, **kwargs):
        if path == candidate / "migration-spec.json":
            return json.dumps(authority._spec(authority.LIVE))
        return read_text(path, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_second_spec_read)
    result, _notes = pkg._assess_package_data_access(
        root, candidate, local, gate_root=root, provider_packages=(), inputs=inputs
    )
    assert observations == [True]
    assert result.state == "local_import_ready"


def test_shipped_role_must_exist_even_when_the_snapshot_is_otherwise_s1_clean(tmp_path: Path) -> None:
    candidate = producer._assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    assert pkg.pri.verify_s1(candidate).integrity.is_clean
    local = {**authority.LOCAL, "shipped": [{"path": "data/missing.csv"}]}
    with pytest.raises(pkg.PackagingError, match="^data_access_shipped_role_missing$"):
        pkg._data_access_inputs(candidate, local, ())


@pytest.mark.parametrize("change", ["extra", "missing", "digest", "root"])
def test_snapshot_comparison_is_not_a_fresh_manifest_baseline(tmp_path: Path, change: str) -> None:
    package = producer._assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    snapshot = pkg._package_snapshot(package)
    assert snapshot is not None and pkg._snapshot_matches(snapshot)
    if change == "extra":
        (package / "extra.txt").write_bytes(b"new")
    elif change == "missing":
        (package / "migration-spec.json").unlink()
    elif change == "digest":
        path = package / "migration-spec.json"
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        replacement = package.with_name("replacement")
        shutil.copytree(package, replacement)
        package.rename(package.with_name("retired"))
        replacement.rename(package)
        assert _files(package) == _files(package.with_name("retired")), "root-only control preserves every byte"
    producer._reseal(package)
    assert pkg.pri.verify_s1(package).integrity.is_clean
    assert not pkg._snapshot_matches(snapshot), f"held snapshot accepted {change}"


@pytest.mark.parametrize("change", ["namespace", "file-digest", "manifest-digest", "lexical-root"])
def test_snapshot_guards_have_independent_negative_controls(tmp_path: Path, change: str) -> None:
    """Each assertion isolates one snapshot check; no second changed file or reseal can mask it."""
    package = producer._assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    snapshot = pkg._package_snapshot(package)
    assert snapshot is not None and pkg._snapshot_matches(snapshot)
    if change == "namespace":
        (package / "extra.txt").write_bytes(b"extra")
    elif change in ("file-digest", "manifest-digest"):
        name = "migration-spec.json" if change == "file-digest" else "package-manifest.json"
        path = package / name
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        snapshot = snapshot._replace(
            verified=replace(snapshot.verified, root_identity=str(package.with_name("foreign")))
        )
    assert not pkg._snapshot_matches(snapshot), f"independent {change} guard did not refuse"


def test_generated_delta_is_exact_and_the_manifest_extends_only_the_held_map(tmp_path: Path) -> None:
    package = producer._assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    snapshot = pkg._package_snapshot(package)
    assert snapshot is not None
    generated = {
        "data-access.json": json.dumps(producer.LOCAL_PROJECTION).encode("utf-8"),
        "README.md": b"generated README",
        "handover.md": b"generated handover",
    }
    manifest = json.loads(snapshot.manifest)
    files = dict(snapshot.digests)
    files.update({key: hashlib.sha256(raw).hexdigest() for key, raw in generated.items()})
    for key, raw in generated.items():
        (package / key).write_bytes(raw)
    generated["package-manifest.json"] = pkg._seal_package(package, manifest, files=files, verify=True)
    assert pkg._snapshot_matches(snapshot, generated), "producer-only delta must be permitted"
    assert pkg.pri.verify_s1(package).integrity.is_clean
    assert {key: digest for key, digest in manifest["contents"]["files"].items() if key not in generated} == dict(
        snapshot.digests
    )
    (package / "README.md").write_bytes(b"not the generated README")
    producer._reseal(package)
    assert pkg.pri.verify_s1(package).integrity.is_clean
    assert not pkg._snapshot_matches(snapshot, generated), "allowlisting a name must not allow arbitrary bytes"


def test_generated_allowlist_cannot_expand_to_spec_or_an_unknown_file(tmp_path: Path) -> None:
    package = producer._assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    snapshot = pkg._package_snapshot(package)
    assert snapshot is not None
    for key in ("migration-spec.json", "other.txt"):
        raw = b"changed"
        (package / key).write_bytes(raw)
        assert not pkg._snapshot_matches(snapshot, {key: raw}), "only the explicit generated roles may change"


def test_final_seal_never_inventories_new_bytes(tmp_path: Path) -> None:
    """Final S1 sees an extra file because the final manifest must extend the held map."""
    package = producer._assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    snapshot = pkg._package_snapshot(package)
    assert snapshot is not None
    manifest = json.loads(snapshot.manifest)
    (package / "unexpected.txt").write_bytes(b"never declared by the provisional S1 snapshot")
    generated = {"data-access.json": json.dumps(producer.LOCAL_PROJECTION).encode("utf-8")}
    with pytest.raises(pkg.PackagingError, match="^data_access_final_integrity_failed$"):
        pkg._write_data_access_final(package, manifest, snapshot, generated)
    final_manifest = json.loads((package / "package-manifest.json").read_bytes())
    assert "unexpected.txt" not in final_manifest["contents"]["files"]


def test_final_s1_refusal_is_not_masked_by_another_snapshot_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct seal control isolates final S1 from the later S2/snapshot rechecks."""
    package = producer._assessment_candidate(tmp_path, authority._spec(authority.FLAT))
    snapshot = pkg._package_snapshot(package)
    assert snapshot is not None
    manifest = json.loads(snapshot.manifest)
    failed = replace(snapshot.verified, integrity=replace(snapshot.verified.integrity, status="unassessable"))
    monkeypatch.setattr(pkg.pri, "verify_s1", lambda _root: failed)
    with pytest.raises(pkg.PackagingError, match="^data_access_final_integrity_failed$"):
        pkg._seal_package(package, manifest, files=dict(snapshot.digests), verify=True)


def test_final_s2_receives_the_exact_original_ordered_cohort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider ordinals mean nothing if final S2 silently reorders or narrows the request."""
    provider = producer._direct_provider(tmp_path / "provider")
    consumer = producer._provider_consumer(tmp_path, provider)
    inputs = pkg._data_access_inputs(consumer, copy.deepcopy(authority.LOCAL), (provider,))
    verify = pkg.pri.verify_phase1_role_identity
    calls = []

    def same_cohort(roots):
        assert roots == inputs.roots, "final S2 must use the original ordered cohort"
        calls.append(True)
        return verify(roots)

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", same_cohort)
    pkg._final_data_access_check(inputs, {})
    assert calls == [True], "final S2 must actually run"


@pytest.mark.parametrize("phase", ["before-final-seal", "after-final-s2"])
def test_candidate_stays_pinned_through_the_entire_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    bundle, out, options = _local_bundle(tmp_path)
    pkg.package_unit(bundle, UNIT, out, **options)
    prior = _files(out / UNIT)
    hit = []
    if phase == "before-final-seal":
        seal = pkg._seal_package

        def change_before_seal(dest, result, **kwargs):
            if kwargs.get("verify"):
                (dest / "unexpected.txt").write_bytes(b"unexpected after assessment")
                hit.append(True)
            return seal(dest, result, **kwargs)

        monkeypatch.setattr(pkg, "_seal_package", change_before_seal)
    else:
        verify = pkg.pri.verify_phase1_role_identity
        rounds = []

        def change_after_final_s2(roots, **kwargs):
            result = verify(roots, **kwargs)
            rounds.append(True)
            if len(rounds) == 2:
                path = roots[-1] / "README.md"
                path.write_bytes(b"unexpected after final S2")
                producer._reseal(roots[-1])
                assert pkg.pri.verify_s1(roots[-1]).integrity.is_clean
                hit.append(True)
            return result

        monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", change_after_final_s2)
    code = "data_access_final_integrity_failed" if phase == "before-final-seal" else "data_access_candidate_changed"
    with pytest.raises(pkg.PackagingError, match=f"^{code}$"):
        pkg.package_unit(bundle, UNIT, out, **options)
    assert hit == [True]
    assert _files(out / UNIT) == prior
    assert not pkg.staging_dir(out, UNIT).exists()


def test_foreign_projection_after_assessment_cannot_be_overwritten_as_an_allowed_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowlist covers producer writes, not somebody else's bytes overwritten by them."""
    bundle, out, options = _local_bundle(tmp_path)
    assess = pkg._assess_package_data_access
    hits = []

    def change_after_assessment(bundle_arg, candidate, local, **kwargs):
        assessment, notes = assess(bundle_arg, candidate, local, **kwargs)
        assert assessment.state == "local_import_ready"
        (candidate / "data-access.json").write_bytes(b"not producer generated")
        hits.append(True)
        return assessment, notes

    monkeypatch.setattr(pkg, "_assess_package_data_access", change_after_assessment)
    with pytest.raises(pkg.PackagingError, match="^data_access_candidate_changed$"):
        pkg.package_unit(bundle, UNIT, out, **options)
    assert hits == [True]
    assert not (out / UNIT).exists()


def test_final_s1_is_required_even_when_candidate_bytes_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, out, options = _local_bundle(tmp_path)
    verify = pkg.pri.verify_s1
    checks = []

    def cannot_verify_final(root):
        result = verify(root)
        path = root / "data-access.json"
        if path.exists() and json.loads(path.read_bytes())["state"] == "local_import_ready":
            checks.append(True)
            return replace(result, integrity=replace(result.integrity, status="unassessable"))
        return result

    monkeypatch.setattr(pkg.pri, "verify_s1", cannot_verify_final)
    with pytest.raises(pkg.PackagingError, match="^data_access_final_integrity_failed$"):
        pkg.package_unit(bundle, UNIT, out, **options)
    assert checks == [True]
    assert not (out / UNIT).exists()


def test_final_s2_checks_the_final_bytes_without_reassessing_or_reselecting_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, out, options = _local_bundle(tmp_path)
    provider = producer._direct_provider(tmp_path / "provider")
    verify = pkg.pri.verify_phase1_role_identity
    observed = []

    def verify_final(roots, **kwargs):
        manifest = json.loads((roots[-1] / "package-manifest.json").read_bytes())
        projection = json.loads((roots[-1] / "data-access.json").read_bytes())
        assert (
            manifest["contents"]["files"]["data-access.json"]
            == hashlib.sha256((roots[-1] / "data-access.json").read_bytes()).hexdigest()
        )
        if projection["state"] != "cannot_establish":
            for name in ("README.md", "handover.md"):
                assert b"state=local_import_ready" in (roots[-1] / name).read_bytes()
        observed.append((tuple(roots), projection["state"]))
        return verify(roots, **kwargs)

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", verify_final)
    pkg.package_unit(bundle, UNIT, out, **options, provider_packages=(provider,))
    expected_roots = (provider, pkg.staging_dir(out, UNIT))
    assert observed == [(expected_roots, "cannot_establish"), (expected_roots, "local_import_ready")]


@pytest.mark.parametrize("change", ["verdict", "policy", "ordinal"])
def test_final_s2_reselection_or_policy_drift_refuses(tmp_path: Path, change: str) -> None:
    provider = producer._direct_provider(tmp_path / "provider")
    consumer = producer._provider_consumer(tmp_path, provider)
    inputs = pkg._data_access_inputs(consumer, copy.deepcopy(authority.LOCAL), (provider,))
    assert inputs.roles[-1].is_start_ready
    old = inputs.roles[-1]
    if change == "verdict":
        altered = replace(old, verdict="BLOCKED", blockers=("fixture-final-S2-refused",))
    elif change == "policy":
        altered = replace(old, brief_policy=pkg.pri.BriefPolicy("report_only_shared_model", "model_only_unvalidated"))
    else:
        altered = replace(old, dependencies=(replace(old.dependencies[0], provider_ordinal=1),))
        assert altered == old, "ordinal is intentionally absent from ordinary dataclass equality"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(pkg.pri, "verify_phase1_role_identity", lambda roots: (*inputs.roles[:-1], altered))
        with pytest.raises(pkg.PackagingError, match="^data_access_final_roles_changed$"):
            pkg._final_data_access_check(inputs, {})


@pytest.mark.parametrize("change", ["projection", "projection-reseal", "manifest-digest", "manifest-bytes", "root"])
def test_provider_projection_and_root_stay_bound_to_the_pre_s2_snapshot(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    provider = producer._direct_provider(tmp_path / "provider")
    consumer = producer._provider_consumer(tmp_path, provider)
    assert producer._assess_candidate(consumer, root, providers=(provider,)).state == "provider_inherited"
    verify = pkg.pri.verify_phase1_role_identity
    hit = []

    def replace_provider(roots, **kwargs):
        results = verify(roots, **kwargs)
        assert results[-1].is_start_ready
        if change == "root":
            replacement = provider.with_name("replacement")
            shutil.copytree(provider, replacement)
            provider.rename(provider.with_name("retired"))
            replacement.rename(provider)
            assert _files(provider) == _files(provider.with_name("retired"))
        elif change.startswith("projection"):
            path = provider / "data-access.json"
            payload = json.loads(path.read_bytes())
            payload["source_keys"] = [authority.OTHER_KEY]
            path.write_text(json.dumps(payload), encoding="utf-8")
            if change.endswith("reseal"):
                producer._reseal(provider)
        elif change == "manifest-digest":
            path = provider / "package-manifest.json"
            payload = json.loads(path.read_bytes())
            payload["contents"]["files"]["data-access.json"] = "0" * 64
            path.write_text(json.dumps(payload), encoding="utf-8")
        else:
            path = provider / "package-manifest.json"
            path.write_bytes(path.read_bytes() + b"\n")
        hit.append(True)
        return results

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", replace_provider)
    assessment = producer._assess_candidate(consumer, root, providers=(provider,))
    assert hit == [True]
    assert (assessment.state, assessment.codes) == ("cannot_establish", ("projection-invalid",))
    assert assessment.provider_unit is None and assessment.source_keys == ()


def test_provider_declared_digest_is_checked_at_the_held_byte_read(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = producer._direct_provider(tmp_path / "provider")
    consumer = producer._provider_consumer(tmp_path, provider)
    read = Path.read_bytes
    hits = []
    replacement = {**producer.LOCAL_PROJECTION, "source_keys": []}
    raw = json.dumps(replacement).encode("utf-8")

    def different_read(path):
        if path == provider / "data-access.json":
            hits.append(True)
            return raw
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", different_read)
    result = producer._assess_candidate(consumer, root, providers=(provider,))
    assert hits == [True]
    assert (result.state, result.codes) == ("cannot_establish", ("provider-missing",))
    assert result.provider_unit is None, "a clean S1 on disk cannot bless different held bytes"


def test_provider_remains_pinned_after_final_s2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A final S2 result cannot bless a provider replaced just after that verification."""
    provider = producer._direct_provider(tmp_path / "provider")
    consumer = producer._provider_consumer(tmp_path, provider)
    inputs = pkg._data_access_inputs(consumer, copy.deepcopy(authority.LOCAL), (provider,))
    verify = pkg.pri.verify_phase1_role_identity

    def changed_after_final_s2(roots):
        result = verify(roots)
        path = provider / "data-access.json"
        payload = json.loads(path.read_bytes())
        payload["source_keys"] = [authority.OTHER_KEY]
        path.write_text(json.dumps(payload), encoding="utf-8")
        producer._reseal(provider)
        return result

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", changed_after_final_s2)
    with pytest.raises(pkg.PackagingError, match="^data_access_provider_changed$"):
        pkg._final_data_access_check(inputs, {})


def test_only_the_once_parsed_provider_assessment_crosses_s2_selection(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = producer._direct_provider(tmp_path / "provider")
    consumer = producer._provider_consumer(tmp_path, provider)
    capture = pkg._held_projection
    verify = pkg.pri.verify_phase1_role_identity
    assess = pkg.data_access.assess_data_access
    held = []
    selected = []

    def capture_once(manifest, raw):
        assessment, code = capture(manifest, raw)
        if manifest["kind"] == "datasource":
            held.append(assessment)
        return assessment, code

    def after_capture(roots, **kwargs):
        assert len(held) == 1 and held[0] is not None, "provider parsing must precede S2"
        return verify(roots, **kwargs)

    def exact_assessment(gate_root, **kwargs):
        assert kwargs["provider"][1] is held[0], "inherit the held object, not a replacement parse"
        selected.append(True)
        return assess(gate_root, **kwargs)

    monkeypatch.setattr(pkg, "_held_projection", capture_once)
    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", after_capture)
    monkeypatch.setattr(pkg.data_access, "assess_data_access", exact_assessment)
    result = producer._assess_candidate(consumer, root, providers=(provider,))
    assert result.state == "provider_inherited"
    assert selected == [True] and len(held) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "null",
        "string",
        "list",
        "class-missing",
        "class-wrong",
        "class-case",
        "mode-missing",
        "mode-wrong",
        "nested-connection",
        "nested-list",
        "empty-list",
        "row-list",
        "row-unknown-scalar",
        "unknown-leg",
        "unknown-scalar",
        "nested-scalar",
        "table-connection",
        "field-connection-list",
        "join-direct-class",
        "published-missing",
        "published-null",
        "published-empty",
        "published-nested",
        "published-scalar-type",
        "additional-direct",
        "additional-null",
        "additional-scalar",
        "additional-malformed",
    ],
)
def test_published_only_rows_are_complete_before_any_inheritance(
    tmp_path: Path, root: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    provider = producer._direct_provider(tmp_path / "provider")
    consumer = producer._provider_consumer(tmp_path, provider)
    assert producer._assess_candidate(consumer, root, providers=(provider,)).state == "provider_inherited"
    spec = json.loads((consumer / "migration-spec.json").read_bytes())
    row = spec["data_sources"][0]
    if fault == "missing":
        row.pop("connection")
    elif fault in ("null", "string", "list"):
        row["connection"] = {"null": None, "string": "sqlproxy", "list": []}[fault]
    elif fault == "class-missing":
        row["connection"].pop("class")
    elif fault in ("class-wrong", "class-case"):
        row["connection"]["class"] = "sqlserver" if fault == "class-wrong" else "SQLPROXY"
    elif fault == "mode-missing":
        row["connection"].pop("mode")
    elif fault == "mode-wrong":
        row["connection"]["mode"] = True
    elif fault == "nested-connection":
        row["connection"]["connection"] = dict(authority.LIVE)
    elif fault in ("nested-list", "empty-list"):
        row["connection"]["connections"] = [dict(authority.LIVE)] if fault == "nested-list" else []
    elif fault == "row-list":
        row["connections"] = [dict(authority.LIVE)]
    elif fault == "row-unknown-scalar":
        row["unknown-leg"] = "unclassified"
    elif fault == "unknown-leg":
        row["connection"]["unknown-leg"] = dict(authority.LIVE)
    elif fault == "unknown-scalar":
        row["connection"]["unknown-leg"] = "sqlserver"
    elif fault == "nested-scalar":
        row["connection"]["server"] = dict(authority.LIVE)
    elif fault == "table-connection":
        row["tables"] = [{"connection": dict(authority.LIVE)}]
    elif fault == "field-connection-list":
        row["fields"] = [{"metadata": {"connections": [dict(authority.LIVE)]}}]
    elif fault == "join-direct-class":
        row["joins"] = [{"left": {"class": "unknown", "server": "source.example"}}]
    elif fault == "published-missing":
        row.pop("published_datasource")
    elif fault in ("published-null", "published-empty"):
        row["published_datasource"] = None if fault == "published-null" else {}
    elif fault == "published-nested":
        row["published_datasource"]["connection"] = dict(authority.LIVE)
    elif fault == "published-scalar-type":
        row["published_datasource"]["id"] = True
    else:
        extra = {
            "additional-direct": {"connection": dict(authority.LIVE)},
            "additional-null": None,
            "additional-scalar": "sqlproxy",
            "additional-malformed": {"published_datasource": {"luid": producer.DS_LUID}},
        }
        spec["data_sources"].append(extra[fault])
    s2._write(consumer / "migration-spec.json", spec)
    producer._reseal(consumer)
    original_rows = copy.deepcopy(spec["data_sources"])
    assert not pkg._published_only_sources(spec), f"published-only guard accepted {fault}"
    assert spec["data_sources"] == original_rows, "malformed rows cannot shrink the denominator"

    def never_inherit(*_args, **_kwargs):
        pytest.fail("malformed published rows reached provider inheritance")

    monkeypatch.setattr(pkg, "_selected_data_provider", never_inherit)
    result = producer._assess_candidate(consumer, root, providers=(provider,))
    assert result.state == "cannot_establish"
    assert result.provider_unit is None and result.max_phase2_claim == "none"


def test_published_metadata_without_connection_legs_is_not_mistaken_for_a_source() -> None:
    """Business fields named 'class'/'connection' are metadata values, not connection declarations."""
    spec = {
        "data_sources": [
            {
                "id": "published",
                "connection": {"class": "sqlproxy", "mode": "extract", "note": "published source"},
                "published_datasource": {"key": "site/source", "id": None},
                "tables": [{"id": "t", "name": "Rows", "source_relation": "table"}],
                "fields": [{"name": "class", "caption": "connection", "kind": "column", "datatype": "string"}],
                "joins": [],
            }
        ]
    }
    assert pkg._published_only_sources(spec)
