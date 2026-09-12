"""Direct controls for issue #622's neutral-root BOUND/UNVALIDATED producer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

# Direct authority controls intentionally exercise private production seams and import sibling test fixtures.
# pylint: disable=protected-access,missing-function-docstring,wrong-import-position
# pylint: disable=too-many-arguments,too-many-positional-arguments

import current_artifact_revision as revision  # noqa: E402  # pylint: disable=wrong-import-position
import package_unit as pkg  # noqa: E402  # pylint: disable=wrong-import-position
import set_data_folder as folder  # noqa: E402  # pylint: disable=wrong-import-position
from test_package_data_access_snapshot import _files, _local_bundle  # noqa: E402
from test_package_filesystem import link_directory  # noqa: E402
from test_package_role_identity import datasource_package, seal  # noqa: E402
from test_package_start_handoffs import LOCAL_PROJECTION, with_data_access  # noqa: E402
from test_package_unit_gates import DS_UNIT, UNIT, _brief  # noqa: E402
from test_package_unit_reproductions import _shared_bundle  # noqa: E402


def _allow_test_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise every other root predicate while keeping pytest's own root out of this control."""
    monkeypatch.setattr(pkg, "_known_private_roots", lambda: ())


def _portable_owned(root: Path) -> Path:
    bundle, _unused, options = _local_bundle(root)
    out = root / "neutral" / "packages"
    pkg.package_unit(bundle, UNIT, out, **options)
    return out / UNIT


def _portable_shared(root: Path) -> tuple[Path, Path]:
    bundle, oracle = _shared_bundle(root)
    out = root / "neutral" / "packages"
    options = {"oracle_dir": oracle, "assets_dir": bundle.parent / "assets"}
    pkg.package_unit(bundle, DS_UNIT, out, brief=_brief(root, DS_UNIT, "model_only"), **options)
    provider = out / DS_UNIT
    pkg.package_unit(
        bundle,
        UNIT,
        out,
        brief=_brief(root, UNIT, "report_only_shared_model"),
        provider_packages=(provider,),
        **options,
    )
    return provider, out / UNIT


def _manifest(package: Path) -> dict:
    return json.loads((package / pkg.MANIFEST_NAME).read_text(encoding="utf-8"))


def _reseal(package: Path) -> None:
    seal(package, **_manifest(package))


def _expression(package: Path) -> Path:
    matches = list(package.glob(f"fabric/*.SemanticModel/definition/{pkg.EXPRESSIONS_TMDL}"))
    assert len(matches) == 1
    return matches[0]


def _expect_binding_error(
    call,
    *,
    state: str,
    authority: str,
    code: str,
) -> pkg.BindingTransitionError:
    with pytest.raises(pkg.BindingTransitionError) as caught:
        call()
    assert (caught.value.state, caught.value.authority, caught.value.code) == (state, authority, code)
    return caught.value


def test_canonical_root_identity_aliases_and_domain_are_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    windows = pkg._canonical_root_identity(r"C:\T2P\Unit")
    assert windows == pkg._canonical_root_identity("c:/t2p/./unit/")
    posix = pkg._canonical_root_identity("/t2p/./Unit/")
    assert posix == pkg._canonical_root_identity("/t2p/Unit")
    assert posix != pkg._canonical_root_identity("/t2p/unit")
    assert windows != pkg._canonical_root_identity(r"C:\T2P\Other")
    expected = "sha256:" + hashlib.sha256(b"t2p-neutral-root-v1\0c:/t2p/unit").hexdigest()
    assert windows == expected

    monkeypatch.setattr(pkg, "BINDING_ROOT_DOMAIN", b"mutated-domain\0")
    assert pkg._canonical_root_identity(r"C:\T2P\Unit") != expected


def test_explicit_root_policy_accepts_both_native_examples_and_package_layouts() -> None:
    assert pkg._binder_root_allowed(PureWindowsPath(r"C:\t2p"))
    assert pkg._binder_root_allowed(PurePosixPath("/t2p"))
    assert pkg._binder_root_allowed(PureWindowsPath(r"C:\work\_runs\001-x\packages\Unit"))
    assert pkg._binder_root_allowed(PurePosixPath("/work/_runs/001-x/packages/batch/Unit"))
    assert not pkg._binder_root_allowed(PureWindowsPath(r"C:\work\arbitrary\deep\Unit"))
    assert not pkg._binder_root_allowed(PurePosixPath("/work/arbitrary/deep/Unit"))


def test_native_existing_root_is_classified_without_persisting_its_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "packages" / "Unit"
    root.mkdir(parents=True)
    _allow_test_root(monkeypatch)

    result = pkg._neutral_root(root)

    assert result.path == root.resolve()
    assert pkg.BINDING_DIGEST_RE.fullmatch(result.identity)
    assert str(root) not in result.identity


def test_current_profile_home_and_temp_hierarchies_are_all_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    home = tmp_path / "home"
    temporary = tmp_path / "temporary"
    monkeypatch.setattr(Path, "home", lambda: profile)
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TEMP", str(temporary))
    monkeypatch.setenv("TMP", str(temporary))

    roots = set(pkg._known_private_roots())

    assert pkg._canonical_root_spelling(str(profile)) in roots
    assert pkg._canonical_root_spelling(str(home)) in roots
    assert pkg._canonical_root_spelling(str(temporary)) in roots


def test_private_unc_foreign_and_policy_roots_name_the_refusing_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "packages" / "Unit"
    private.mkdir(parents=True)
    monkeypatch.setattr(pkg, "_known_private_roots", lambda: (pkg._canonical_root_spelling(str(tmp_path)),))
    error = _expect_binding_error(
        lambda: pkg._neutral_root(private),
        state=pkg.STATUS_BLOCKED,
        authority="neutral_root",
        code="binding_root_private_hierarchy",
    )
    assert str(private) not in str(error) and Path.home().name not in str(error)

    unc = PureWindowsPath(r"\\server\share\Unit")
    _expect_binding_error(
        lambda: pkg._neutral_root(unc),  # type: ignore[arg-type]
        state=pkg.STATUS_BLOCKED,
        authority="neutral_root",
        code="binding_root_unc",
    )
    foreign = PurePosixPath("/t2p") if os.name == "nt" else PureWindowsPath(r"C:\t2p")
    _expect_binding_error(
        lambda: pkg._neutral_root(foreign),  # type: ignore[arg-type]
        state=pkg.STATUS_BLOCKED,
        authority="neutral_root",
        code="binding_root_foreign_flavour",
    )

    arbitrary = tmp_path / "arbitrary" / "deep" / "Unit"
    arbitrary.mkdir(parents=True)
    _allow_test_root(monkeypatch)
    _expect_binding_error(
        lambda: pkg._neutral_root(arbitrary),
        state=pkg.STATUS_BLOCKED,
        authority="neutral_root",
        code="binding_root_policy_refused",
    )


def test_reparse_root_is_refused_before_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "packages" / "Alias"
    alias.parent.mkdir()
    link_directory(alias, target)
    _allow_test_root(monkeypatch)
    try:
        _expect_binding_error(
            lambda: pkg._neutral_root(alias),
            state=pkg.STATUS_BLOCKED,
            authority="neutral_root",
            code="binding_root_reparse",
        )
    finally:
        if os.name == "nt":
            alias.rmdir()
        else:
            alias.unlink()


def test_missing_original_package_is_cannot_establish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "packages" / "Missing"
    _allow_test_root(monkeypatch)
    _expect_binding_error(
        lambda: pkg.rebind_package(missing),
        state="CANNOT_ESTABLISH",
        authority="neutral_root",
        code="binding_root_missing",
    )


def test_planner_is_write_free_exact_and_preserves_tail_and_separator(tmp_path: Path) -> None:
    package = tmp_path / "package"
    expression = package / "fabric" / "Model.SemanticModel" / "definition" / pkg.EXPRESSIONS_TMDL
    expression.parent.mkdir(parents=True)
    original = (
        'expression DataFolder = "<PACKAGE_ROOT>\\data\\Nested.Data\\" meta [IsParameterQuery=true]\n'
        'expression Ordinary = "not-a-path"\n'
    ).encode()
    expression.write_bytes(original)
    destination = PureWindowsPath(r"C:\t2p\Unit") if os.name == "nt" else PurePosixPath("/t2p/Unit")

    plan = folder.plan_package_rewrite(package, str(destination))

    member = f"fabric/Model.SemanticModel/definition/{pkg.EXPRESSIONS_TMDL}"
    assert expression.read_bytes() == original
    assert plan.affected_members == (member,)
    assert [(row.member, row.old_value) for row in plan.values] == [(member, "<PACKAGE_ROOT>\\data\\Nested.Data\\")]
    separator = "\\" if os.name == "nt" else "/"
    assert plan.values[0].new_value == f"{destination}{separator}data{separator}Nested.Data{separator}"
    assert b'expression Ordinary = "not-a-path"' in plan.members[0].rewritten


def test_real_portable_package_binds_reseals_and_changes_only_target_tmdl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    before = _files(package)
    original_dispatch = _manifest(package)["dispatch_readiness"]

    result = pkg.rebind_package(package)
    after = _files(package)
    block = result["binding"]

    expression = _expression(package).relative_to(package).as_posix()
    assert {name for name in before if before[name] != after[name]} == {expression, pkg.MANIFEST_NAME}
    assert set(before) == set(after)
    assert block == {
        "state": "BOUND",
        "privacy_policy": "neutral_root",
        "canonical_root_identity": pkg._canonical_root_identity(str(package.resolve())),
        "manifest_excluded_revision": revision.package_manifest_excluded_revision(package),
        "bound_members": [expression],
        "data_access_authority": {
            "identity": "sha256:" + hashlib.sha256((package / "data-access.json").read_bytes()).hexdigest(),
            "state": "local_import_ready",
            "validation": "validated",
            "scope": "model_and_report",
            "provider_cohort": [],
        },
    }
    assert pkg.pri.verify_s1(package).integrity.is_clean
    assert _manifest(package)["dispatch_readiness"] == original_dispatch
    assert _manifest(package)["construction_status"] == pkg.STATUS_ASSEMBLED
    serialized = json.dumps(block)
    assert str(package) not in serialized and Path.home().name not in serialized
    assert "START_READY" not in serialized


def test_staged_and_published_s1_revision_checks_both_execute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    observed: list[tuple[bool, str]] = []
    verify = pkg._verify_bound_candidate

    def record(*args, published: bool, **kwargs) -> None:
        verify(*args, published=published, **kwargs)
        root = args[0]
        assert pkg.pri.verify_s1(root).integrity.is_clean
        block = _manifest(root)["binding"]
        current = revision.package_manifest_excluded_revision(root)
        assert current == block["manifest_excluded_revision"]
        observed.append((published, current))

    monkeypatch.setattr(pkg, "_verify_bound_candidate", record)
    pkg.rebind_package(package)

    assert [published for published, _digest in observed] == [False, False, True]
    assert len({digest for _published, digest in observed}) == 1


def test_set_data_folder_package_routes_to_package_unit_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="OK - BOUND/UNVALIDATED root_identity=sha256:" + "a" * 64 + " members=1\n",
            stderr="",
        )

    monkeypatch.setattr(folder.subprocess, "run", run)
    assert folder._package(Path("package"), (Path("provider"),)) == 0
    command, kwargs = calls[0]
    assert Path(command[1]).name == "package_unit.py"
    assert command[2] == "--bind-package" and Path(command[3]).is_absolute()
    assert command[4] == "--provider-package" and Path(command[5]).is_absolute()
    assert [Path(command[3]).name, Path(command[5]).name] == ["package", "provider"]
    assert kwargs == {"capture_output": True, "text": True, "check": False}


@pytest.mark.parametrize(
    ("state", "codes", "expected_state", "expected_code"),
    [
        ("blocked", ["marker-only"], "BLOCKED", "marker-only"),
        ("blocked", ["stale-clear"], "BLOCKED", "stale-clear"),
        ("cannot_establish", ["audit-missing"], "CANNOT_ESTABLISH", "audit-missing"),
    ],
)
def test_blocked_or_unestablished_data_access_remains_unbound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    codes: list[str],
    expected_state: str,
    expected_code: str,
) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    payload = {
        "schema": "phase1-data-access/v1",
        "state": state,
        "source_keys": [],
        "provider_unit": None,
        "provider_state": None,
        "validation": "not_established",
        "effective_scope": None,
        "max_phase2_claim": "none",
        "codes": codes,
    }
    (package / "data-access.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    _reseal(package)
    before = _files(package)

    _expect_binding_error(
        lambda: pkg.rebind_package(package),
        state=expected_state,
        authority="data_access",
        code=expected_code,
    )
    assert _files(package) == before and "binding" not in _manifest(package)


def test_missing_data_access_authority_names_the_verified_member_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    (package / "data-access.json").unlink()
    manifest = _manifest(package)
    manifest["artifacts"]["data_access"] = None
    seal(package, **manifest)

    _expect_binding_error(
        lambda: pkg.rebind_package(package),
        state=pkg.STATUS_BLOCKED,
        authority="data_access",
        code=pkg.pfs.CODE_MEMBER_UNVERIFIED,
    )


def test_authorized_model_only_binding_remains_unvalidated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, _consumer = _portable_shared(tmp_path)
    _allow_test_root(monkeypatch)
    brief = provider / "migration-brief.md"
    brief.write_text(
        brief.read_text(encoding="utf-8").replace('"stop"', '"model_only_unvalidated"'),
        encoding="utf-8",
    )
    projection = {
        "schema": "phase1-data-access/v1",
        "state": "authorized_model_only",
        "source_keys": ["source-key:ab1baa4b3f77bb70"],
        "provider_unit": None,
        "provider_state": None,
        "validation": "unvalidated",
        "effective_scope": "model_only",
        "max_phase2_claim": "structural_only",
        "codes": ["brief-model-only", "human-authorize"],
    }
    (provider / "data-access.json").write_text(json.dumps(projection, indent=2) + "\n", encoding="utf-8")
    _reseal(provider)

    result = pkg.rebind_package(provider)

    authority = result["binding"]["data_access_authority"]
    assert (authority["state"], authority["validation"]) == ("authorized_model_only", "unvalidated")
    assert result["dispatch_readiness"]["status"] == pkg.DISPATCH_READINESS_NOT_EVALUATED


def test_standalone_datasource_bind_request_is_refused_by_topology(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = with_data_access(
        datasource_package(tmp_path / "neutral" / "packages" / "Standalone", unit="Standalone", luid=None),
        projection=LOCAL_PROJECTION,
    )
    _allow_test_root(monkeypatch)

    _expect_binding_error(
        lambda: pkg.rebind_package(package),
        state=pkg.STATUS_BLOCKED,
        authority="topology",
        code="datasource_only_no_bind",
    )
    assert "binding" not in _manifest(package)


def test_provider_binds_first_then_consumer_records_exact_cohort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, consumer = _portable_shared(tmp_path)
    _allow_test_root(monkeypatch)

    provider_result = pkg.rebind_package(provider)
    consumer_result = pkg.rebind_package(consumer, provider_packages=(provider,))

    provider_block = provider_result["binding"]
    consumer_block = consumer_result["binding"]
    cohort = consumer_block["data_access_authority"]["provider_cohort"]
    expected_identity = pkg.data_access.provider_reference(_manifest(provider)["unit"])
    assert provider_block["state"] == "BOUND"
    assert cohort == [
        {
            "identity": expected_identity,
            "revision": provider_block["manifest_excluded_revision"],
            "ordinal": 0,
        }
    ]
    assert pkg.verify_bound_package(consumer, provider_packages=(provider,)) == consumer_block


def test_consumer_without_its_provider_remains_unbound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, consumer = _portable_shared(tmp_path)
    _allow_test_root(monkeypatch)
    pkg.rebind_package(provider)

    error = _expect_binding_error(
        lambda: pkg.rebind_package(consumer),
        state="CANNOT_ESTABLISH",
        authority="provider",
        code=pkg.pri.CODE_PROVIDER_MISSING,
    )
    assert str(provider) not in str(error) and "binding" not in _manifest(consumer)


def test_extra_provider_makes_the_consumer_cohort_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, consumer = _portable_shared(tmp_path)
    _allow_test_root(monkeypatch)
    pkg.rebind_package(provider)
    duplicate = provider.parent / "batch" / provider.name
    duplicate.parent.mkdir()
    shutil.copytree(provider, duplicate)
    pkg.rebind_package(duplicate)

    _expect_binding_error(
        lambda: pkg.rebind_package(consumer, provider_packages=(provider, duplicate)),
        state="CANNOT_ESTABLISH",
        authority="provider",
        code=pkg.pri.CODE_PROVIDER_AMBIGUOUS,
    )


def test_changed_provider_after_snapshot_refuses_before_consumer_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider, consumer = _portable_shared(tmp_path)
    _allow_test_root(monkeypatch)
    pkg.rebind_package(provider)
    before = _files(consumer)
    allowed_diff = pkg._assert_binding_allowed_diff
    changed = []

    def mutate_provider(*args, **kwargs) -> None:
        allowed_diff(*args, **kwargs)
        if not changed:
            readme = provider / "README.md"
            readme.write_bytes(readme.read_bytes() + b"\nchanged provider\n")
            _reseal(provider)
            changed.append(True)

    monkeypatch.setattr(pkg, "_assert_binding_allowed_diff", mutate_provider)
    error = _expect_binding_error(
        lambda: pkg.rebind_package(consumer, provider_packages=(provider,)),
        state=pkg.STATUS_BLOCKED,
        authority="provider",
        code="binding_provider_revision_changed",
    )
    assert changed == [True] and str(provider) not in str(error)
    assert _files(consumer) == before


def test_changed_provider_ordinal_refuses_the_staged_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    provider, consumer = _portable_shared(tmp_path)
    _allow_test_root(monkeypatch)
    pkg.rebind_package(provider)
    before = _files(consumer)
    verify = pkg.pri.verify_phase1_role_identity
    calls = []

    def changed_ordinal(roots, **kwargs):
        results = verify(roots, **kwargs)
        calls.append(tuple(roots))
        if len(calls) >= 2:
            candidate = results[-1]
            dependency = replace(candidate.dependencies[0], provider_ordinal=1)
            results = (*results[:-1], replace(candidate, dependencies=(dependency,)))
        return results

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", changed_ordinal)
    _expect_binding_error(
        lambda: pkg.rebind_package(consumer, provider_packages=(provider,)),
        state="CANNOT_ESTABLISH",
        authority="provider",
        code="binding_provider_ordinal_invalid",
    )
    assert _files(consumer) == before


@pytest.mark.parametrize("change", ["extra-file", "other-tmdl"])
def test_unrelated_staged_change_is_refused_by_allowed_diff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    before = _files(package)
    apply = folder.apply_package_rewrite_plan

    def mutate(root: Path, plan) -> None:
        apply(root, plan)
        target = root / (
            "unexpected.txt" if change == "extra-file" else f"fabric/{UNIT}.SemanticModel/definition/model.tmdl"
        )
        target.write_bytes((target.read_bytes() if target.exists() else b"") + b"\nunrelated\n")

    monkeypatch.setattr(folder, "apply_package_rewrite_plan", mutate)
    code = "binding_file_set_changed" if change == "extra-file" else "binding_unrelated_change"
    _expect_binding_error(
        lambda: pkg.rebind_package(package),
        state=pkg.STATUS_BLOCKED,
        authority="allowed_diff",
        code=code,
    )
    assert _files(package) == before


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("unresolved", "package_root_token_unresolved"),
        ("changed", "package_rewrite_old_value_changed"),
        ("missing", "package_rewrite_expression_missing"),
    ],
)
def test_changed_missing_or_unresolved_expression_is_not_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, code: str
) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    expression = _expression(package)
    text = expression.read_text(encoding="utf-8")
    if change == "unresolved":
        text = text.replace("<PACKAGE_ROOT>\\data\\", "<PACKAGE_ROOT>\\missing\\")
    elif change == "changed":
        foreign = "C:\\other\\data\\" if os.name == "nt" else "/other/data/"
        text = text.replace("<PACKAGE_ROOT>\\data\\", foreign)
    else:
        text = ""
    expression.write_text(text, encoding="utf-8")
    _reseal(package)

    _expect_binding_error(
        lambda: pkg.rebind_package(package),
        state=pkg.STATUS_BLOCKED,
        authority="rewrite_plan",
        code=code,
    )


def test_manual_manifest_digest_mutation_is_refused_by_s1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    manifest = _manifest(package)
    manifest["contents"]["files"][_expression(package).relative_to(package).as_posix()] = "0" * 64
    (package / pkg.MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    _expect_binding_error(
        lambda: pkg.rebind_package(package),
        state=pkg.STATUS_BLOCKED,
        authority="s1",
        code=pkg.pfs.CODE_DIGEST_MISMATCH,
    )


def test_moving_requires_rebind_and_sanitizing_invalidates_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    first = pkg.rebind_package(package)["binding"]
    moved = package.parent / "batch" / package.name
    moved.parent.mkdir()
    shutil.move(package, moved)

    _expect_binding_error(
        lambda: pkg.verify_bound_package(moved),
        state=pkg.STATUS_BLOCKED,
        authority="neutral_root",
        code="binding_root_identity_mismatch",
    )
    second = pkg.rebind_package(moved)["binding"]
    assert second["canonical_root_identity"] != first["canonical_root_identity"]
    assert second["manifest_excluded_revision"] != first["manifest_excluded_revision"]

    expression = _expression(moved)
    current = str(moved.resolve())
    expression.write_text(
        expression.read_text(encoding="utf-8").replace(current, pkg.PACKAGE_ROOT_TOKEN),
        encoding="utf-8",
    )
    _reseal(moved)
    _expect_binding_error(
        lambda: pkg.rebind_package(moved),
        state=pkg.STATUS_BLOCKED,
        authority="revision",
        code="binding_manifest_excluded_revision_mismatch",
    )


def test_publication_failure_restores_the_complete_old_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    before = _files(package)
    verify = pkg._verify_bound_candidate
    published_checks = []

    def fail_after_publish(*args, published: bool, **kwargs) -> None:
        verify(*args, published=published, **kwargs)
        if published:
            published_checks.append(True)
            raise pkg.BindingTransitionError(
                "CANNOT_ESTABLISH",
                "publication",
                "forced_publication_failure",
            )

    monkeypatch.setattr(pkg, "_verify_bound_candidate", fail_after_publish)
    _expect_binding_error(
        lambda: pkg.rebind_package(package),
        state="CANNOT_ESTABLISH",
        authority="publication",
        code="forced_publication_failure",
    )
    assert published_checks == [True]
    assert _files(package) == before
    assert not pkg.staging_dir(package.parent, package.name).exists()
    assert not pkg.retired_dir(package).exists()


@pytest.mark.parametrize("residue", ["staged", "retired"])
def test_ambiguous_publication_residue_is_cannot_establish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, residue: str
) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    before = _files(package)
    path = pkg.staging_dir(package.parent, package.name) if residue == "staged" else pkg.retired_dir(package)
    path.mkdir()

    _expect_binding_error(
        lambda: pkg.rebind_package(package),
        state="CANNOT_ESTABLISH",
        authority="transition",
        code="binding_publication_ambiguous",
    )
    assert _files(package) == before


def test_manifest_excluded_revision_ignores_only_the_manifest(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "member.txt").write_bytes(b"one")
    (package / pkg.MANIFEST_NAME).write_bytes(b"first")
    initial = revision.package_manifest_excluded_revision(package)
    (package / pkg.MANIFEST_NAME).write_bytes(b"second")
    assert revision.package_manifest_excluded_revision(package) == initial
    (package / "member.txt").write_bytes(b"two")
    assert revision.package_manifest_excluded_revision(package) != initial


def test_path_budget_violation_names_existing_authority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = _portable_owned(tmp_path)
    _allow_test_root(monkeypatch)
    monkeypatch.setattr(
        pkg,
        "scan_path_ceiling",
        lambda *_args, **_kwargs: {"counted": {"unknown": 0, "measured": 1, "over_ceiling": 1}},
    )

    _expect_binding_error(
        lambda: pkg.rebind_package(package),
        state=pkg.STATUS_BLOCKED,
        authority="path_budget",
        code="binding_path_budget_exceeded",
    )
