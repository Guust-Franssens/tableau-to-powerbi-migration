"""Direct controls for PR #608's producer snapshots, final reseal and published-only inheritance."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

import test_data_access_contract as authority
import test_package_role_identity as s2
import test_package_unit_reproductions as producer
from test_data_access_contract import _root_fixture  # noqa: F401  # shared pytest fixture
from test_package_unit_gates import UNIT, _brief, _bundle, pkg


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
        "unknown-leg",
        "nested-scalar",
        "published-missing",
        "published-null",
        "published-empty",
        "published-nested",
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
    elif fault == "unknown-leg":
        row["connection"]["unknown-leg"] = dict(authority.LIVE)
    elif fault == "nested-scalar":
        row["connection"]["server"] = dict(authority.LIVE)
    elif fault == "published-missing":
        row.pop("published_datasource")
    elif fault in ("published-null", "published-empty"):
        row["published_datasource"] = None if fault == "published-null" else {}
    elif fault == "published-nested":
        row["published_datasource"]["connection"] = dict(authority.LIVE)
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
