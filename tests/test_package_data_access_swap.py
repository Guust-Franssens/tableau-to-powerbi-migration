"""PR #608: the held candidate is checked inside the final guarded publication, never before it."""

from __future__ import annotations

# These controls intentionally exercise the producer's private assembly and publication seams.
# pylint: disable=protected-access

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_package_data_access_snapshot import _binding_package, _binding_provider, _files, _local_bundle
import test_package_unit_reproductions as producer
from test_package_unit_gates import UNIT, pkg


def test_binding_final_inspection_occurs_inside_replace_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _binding_package(tmp_path)
    before, original_id = _files(root), root.lstat().st_ino
    staged, retired = pkg.staging_dir(root.parent, root.name), pkg.retired_dir(root)
    events = []
    rename, inspect, discard = pkg._rename_retrying, pkg._binding_final_inspection, pkg._discard_scratch

    def rename_at(source, destination):
        rename(source, destination)
        if destination in (retired, root):
            events.append("retire" if destination == retired else "candidate")

    def inspect_at(cohort, final, *args):
        assert final == root and root.is_dir() and not staged.exists()
        assert retired.lstat().st_ino == original_id and _files(retired) == before
        assert events == ["retire", "candidate"]
        result = inspect(cohort, final, *args)
        assert result.exit_code == 0 and result.code == "binding_bound"
        assert result._authority.snapshot.verified.root == root
        events.append("final-inspection")
        return result

    def cleanup_at(path):
        if path == retired:
            assert events == ["retire", "candidate", "final-inspection"]
            events.append("cleanup")
        return discard(path)

    monkeypatch.setattr(pkg, "_rename_retrying", rename_at)
    monkeypatch.setattr(pkg, "_binding_final_inspection", inspect_at)
    monkeypatch.setattr(pkg, "_discard_scratch", cleanup_at)
    result = pkg.bind_package(root)
    assert (result.exit_code, result.outcome) == (0, "published")
    assert events == ["retire", "candidate", "final-inspection", "cleanup"]
    assert not staged.exists() and not retired.exists()


@pytest.mark.parametrize("operation", ["bind", "sanitize"])
@pytest.mark.parametrize(
    "seam", ["retire-before", "retire-after", "publish-before", "publish-after", "final-inspection"]
)
@pytest.mark.parametrize("interrupt", [False, True])
def test_binding_rename_and_inspection_failures_restore_the_original_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, seam: str, interrupt: bool
) -> None:
    root = _binding_package(tmp_path)
    if operation == "sanitize":
        assert pkg.bind_package(root).exit_code == 0
    before, original_id = _files(root), root.lstat().st_ino
    staged, retired = pkg.staging_dir(root.parent, root.name), pkg.retired_dir(root)
    rename, inspect = pkg._rename_retrying, pkg._binding_final_inspection
    hits = []

    def fail() -> None:
        hits.append(seam)
        raise KeyboardInterrupt if interrupt else OSError("private-location must not escape")

    def rename_at(source, destination):
        chosen = destination == (retired if seam.startswith("retire") else root) and source in (root, staged)
        if chosen and seam.endswith("before") and not hits:
            fail()
        rename(source, destination)
        if chosen and seam.endswith("after") and not hits:
            fail()

    def inspect_at(*args):
        if seam == "final-inspection":
            fail()
        return inspect(*args)

    monkeypatch.setattr(pkg, "_rename_retrying", rename_at)
    monkeypatch.setattr(pkg, "_binding_final_inspection", inspect_at)
    result = (pkg.bind_package if operation == "bind" else pkg.sanitize_package)(root)
    assert hits == [seam], "the failure must reach its named transaction seam"
    assert result.exit_code == (130 if interrupt else 3)
    assert result.outcome == ("unchanged" if seam == "retire-before" else "rolled-back")
    assert _files(root) == before and root.lstat().st_ino == original_id
    assert not staged.exists() and not retired.exists()
    assert "private-location" not in str(result.as_dict())


@pytest.mark.parametrize("failure", ["returned-residue", "raised", "interrupt-before", "interrupt-after"])
def test_binding_cleanup_failure_cannot_erase_a_verified_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = _binding_package(tmp_path)
    retired = pkg.retired_dir(root)
    discard = pkg._discard_scratch
    committed, hits = [], []

    def fail_cleanup(path):
        if path != retired:
            return discard(path)
        assert pkg.pri.verify_s1(root).integrity.is_clean
        committed.append(_files(root))
        hits.append(failure)
        if failure == "returned-residue":
            return "fixture residue"
        if failure == "interrupt-after":
            assert discard(path) is None
        if failure.startswith("interrupt"):
            raise KeyboardInterrupt
        raise OSError("private cleanup path")

    monkeypatch.setattr(pkg, "_discard_scratch", fail_cleanup)
    result = pkg.bind_package(root)
    assert hits == [failure]
    assert result.exit_code == (130 if failure.startswith("interrupt") else 1)
    assert result.outcome == ("published" if failure == "interrupt-after" else "published-with-residue")
    assert _files(root) == committed[0], "a candidate already committed must not be rolled back by cleanup"
    assert retired.exists() == (failure != "interrupt-after")
    assert pkg.inspect_package(root).code == "binding_bound"
    if retired.exists():
        refused = pkg.bind_package(root)
        assert (refused.exit_code, refused.code) == (1, "binding_transaction_residue")
    monkeypatch.setattr(pkg, "_discard_scratch", discard)
    if retired.exists():
        assert discard(retired) is None


def test_binding_failed_rollback_preserves_both_recovery_trees(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _binding_package(tmp_path)
    before = _files(root)
    staged, retired = pkg.staging_dir(root.parent, root.name), pkg.retired_dir(root)
    rename, hits = pkg._rename_retrying, []

    def cannot_publish_or_restore(source, destination):
        if destination == root:
            hits.append("publish" if source == staged else "rollback")
            raise OSError("fixture rename failure")
        rename(source, destination)

    monkeypatch.setattr(pkg, "_rename_retrying", cannot_publish_or_restore)
    result = pkg.bind_package(root)
    assert (result.exit_code, result.outcome) == (3, "cannot-establish")
    assert hits == ["publish", "rollback"]
    assert not root.exists() and _files(retired) == before and staged.is_dir()
    assert pkg.pri.verify_s1(staged).integrity.is_clean
    rename(retired, root)
    assert pkg._discard_scratch(staged) is None


def test_binding_concurrent_original_reseal_is_preserved_not_rebased(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _binding_package(tmp_path)
    original_id = root.lstat().st_ino
    copy_tree, edits = pkg._copy_binding_tree, []

    def copy_then_edit(tree, staged):
        copy_tree(tree, staged)
        path = root / "migration-spec.json"
        path.write_bytes(path.read_bytes() + b"\n")
        producer._reseal(root)
        assert pkg.pri.verify_s1(root).integrity.is_clean, "a concurrent reseal must not become the binder's baseline"
        edits.append(_files(root))

    monkeypatch.setattr(pkg, "_copy_binding_tree", copy_then_edit)
    result = pkg.bind_package(root)
    assert len(edits) == 1
    assert (result.exit_code, result.code, result.outcome) == (3, "binding_original_changed", "rolled-back")
    assert _files(root) == edits[0] and root.lstat().st_ino == original_id
    assert not pkg.staging_dir(root.parent, root.name).exists() and not pkg.retired_dir(root).exists()


def test_binding_holds_empty_directory_namespace_not_only_s1_file_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _binding_package(tmp_path)
    before = _files(root)
    copied, hits = pkg._copy_binding_tree, []

    def extra_directory(tree, staged):
        copied(tree, staged)
        (root / "new-empty-directory").mkdir()
        assert pkg.pri.verify_s1(root).integrity.is_clean, "S1 deliberately does not hash empty directories"
        hits.append(True)

    monkeypatch.setattr(pkg, "_copy_binding_tree", extra_directory)
    result = pkg.bind_package(root)
    assert hits == [True]
    assert (result.exit_code, result.code, result.outcome) == (3, "binding_original_changed", "rolled-back")
    assert _files(root) == before and (root / "new-empty-directory").is_dir()


def test_binding_final_root_s1_is_required_even_when_the_candidate_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _binding_package(tmp_path)
    before, original_id = _files(root), root.lstat().st_ino
    verify, hits = pkg.pri.verify_s1, []

    def fail_final_s1(path):
        result = verify(path)
        if path == root and root.lstat().st_ino != original_id:
            assert result.integrity.is_clean
            assert pkg.retired_dir(root).is_dir()
            hits.append(True)
            return replace(result, integrity=replace(result.integrity, status="unassessable"))
        return result

    monkeypatch.setattr(pkg.pri, "verify_s1", fail_final_s1)
    result = pkg.bind_package(root)
    assert hits
    assert (result.exit_code, result.code, result.outcome) == (3, "binding_s1_unestablished", "rolled-back")
    assert _files(root) == before and root.lstat().st_ino == original_id


@pytest.mark.parametrize("version", [(3, 11, 9), (3, 12, 3)])
def test_binding_refuses_runtime_that_ignores_private_staging_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
) -> None:
    root = _binding_package(tmp_path)
    before = _files(root)
    make_stage, hits = pkg._make_binding_staging, []

    def old_runtime(staged):
        with monkeypatch.context() as patch:
            patch.setattr(pkg, "os", SimpleNamespace(name="nt"))
            patch.setattr(pkg.sys, "version_info", version)
            hits.append(version)
            make_stage(staged)

    monkeypatch.setattr(pkg, "_make_binding_staging", old_runtime)
    result = pkg.bind_package(root)
    assert hits == [version]
    assert (result.exit_code, result.code) == (3, "binding_private_staging_unavailable")
    assert _files(root) == before and not pkg.staging_dir(root.parent, root.name).exists()


@pytest.mark.parametrize("seam", ["staged", "final"])
def test_binding_rejects_a_resealed_candidate_at_both_publication_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    root = _binding_package(tmp_path)
    before = _files(root)
    stage, rename = pkg._stage_package_binding, pkg._rename_retrying
    hits = []

    def change(candidate):
        path = candidate / "README.md"
        path.write_bytes(path.read_bytes() + b"\nunrelated edit\n")
        producer._reseal(candidate)
        assert pkg.pri.verify_s1(candidate).integrity.is_clean
        hits.append(seam)

    def after_stage(cohort, staged, *args):
        result = stage(cohort, staged, *args)
        if seam == "staged":
            change(staged)
        return result

    def after_rename(source, destination):
        rename(source, destination)
        if seam == "final" and destination == root and source == pkg.staging_dir(root.parent, root.name):
            change(root)

    monkeypatch.setattr(pkg, "_stage_package_binding", after_stage)
    monkeypatch.setattr(pkg, "_rename_retrying", after_rename)
    result = pkg.bind_package(root)
    assert hits == [seam]
    assert (result.exit_code, result.code, result.outcome) == (3, "binding_candidate_changed", "rolled-back")
    assert _files(root) == before


@pytest.mark.parametrize("when", ["staged", "final"])
def test_binding_rechecks_provider_bytes_after_the_last_s2_at_each_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str
) -> None:
    provider = _binding_provider(tmp_path / "provider")
    assert pkg.bind_package(provider).exit_code == 0
    root = _binding_package(tmp_path / "consumer")
    before = _files(root)
    verify, hits = pkg.pri.verify_phase1_role_identity, []
    stage = pkg.staging_dir(root.parent, root.name)

    def mutate(roots, **kwargs):
        result = verify(roots, **kwargs)
        at_final = roots[-1] == root and pkg.retired_dir(root).exists() and root.exists()
        if not hits and ((when == "staged" and roots[-1] == stage) or (when == "final" and at_final)):
            path = next((provider / "data").rglob("*.csv"))
            path.write_bytes(b"value\n33\n")
            producer._reseal(provider)
            assert pkg.pri.verify_s1(provider).integrity.is_clean
            hits.append(when)
        return result

    monkeypatch.setattr(pkg.pri, "verify_phase1_role_identity", mutate)
    result = pkg.bind_package(root, provider_packages=(provider,))
    assert hits == [when]
    assert (result.exit_code, result.code, result.outcome) == (3, "binding_provider_changed", "rolled-back")
    assert _files(root) == before


@pytest.mark.parametrize("when", ["original", "retired"])
@pytest.mark.parametrize("barrier", ["valid-marker", "malformed-marker", "acl-deny", "acl-failed", "acl-unparseable"])
def test_binding_observes_physical_barrier_at_original_and_retired_addresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, when: str, barrier: str
) -> None:
    from test_credential_gate import _barrier_marker  # pylint: disable=import-outside-toplevel

    root = _binding_package(tmp_path)
    before, original_id = _files(root), root.lstat().st_ino
    retired = pkg.retired_dir(root)
    target = root if when == "original" else retired
    inspect = pkg.data_access.inspect_physical_barrier
    hits = []

    def query(arguments):
        if arguments[0] == str(target / "fabric"):
            hits.append(barrier)
            return {
                "acl-deny": (0, "fixture:(DENY)(WD,AD,WA)"),
                "acl-failed": (5, "fixture:(DENY)(WD,AD,WA) private diagnostic"),
                "acl-unparseable": (0, "not an ACL"),
            }[barrier]
        return 0, "fixture:(F)"

    def inspect_at(path):
        if path == target and not hits and barrier.endswith("marker"):
            assert path.lstat().st_ino == original_id, "barrier authority must inspect the original directory"
            _barrier_marker(path) if barrier == "valid-marker" else (path / pkg.data_access.MARKER).write_bytes(
                b"malformed"
            )
            hits.append(barrier)
        return inspect(path)

    monkeypatch.setattr(pkg.data_access, "inspect_physical_barrier", inspect_at)
    if barrier.startswith("acl"):
        monkeypatch.setattr(pkg.data_access.platform, "system", lambda: "Windows")
        monkeypatch.setattr(pkg.data_access, "_icacls", query)
    result = pkg.bind_package(root)
    assert hits == [barrier]
    expected = {
        "valid-marker": (1, "barrier_marker_present"),
        "malformed-marker": (3, "barrier_marker_invalid"),
        "acl-deny": (1, "barrier_acl_deny"),
        "acl-failed": (3, "barrier_acl_query_failed"),
        "acl-unparseable": (3, "barrier_acl_unparseable"),
    }
    assert (result.exit_code, result.code) == expected[barrier]
    assert result.outcome == ("unchanged" if when == "original" else "rolled-back")
    current = _files(root)
    current.pop(pkg.data_access.MARKER, None)
    assert current == before and root.lstat().st_ino == original_id
    assert not retired.exists() and not pkg.staging_dir(root.parent, root.name).exists()


@pytest.mark.parametrize("change", ["spec", "localized_data", "brief", "projection"])
@pytest.mark.parametrize("seam", ["assembly_return", "budget_return", "swap_entry", "retired_check"])
def test_staged_mutation_never_reaches_publication(  # pylint: disable=too-many-locals
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, seam: str
) -> None:
    """Mutations after the old outer check must refuse without ever exposing the staged directory."""
    bundle, out, options = _local_bundle(tmp_path)
    pkg.package_unit(bundle, UNIT, out, **options)
    final = out / UNIT
    staged, retired = pkg.staging_dir(out, UNIT), pkg.retired_dir(final)
    prior = _files(final)
    prior_id = final.lstat().st_ino
    mutations, publications = [], []
    seam_name = {
        "assembly_return": "_assemble_unit",
        "budget_return": "assert_assembled_fits",
        "swap_entry": "replace_dir",
        "retired_check": "_refuse_if_edited",
    }[seam]
    original = getattr(pkg, seam_name)
    rename = pkg._rename_retrying

    def mutate() -> None:
        assert not mutations, "the intended boundary must be crossed exactly once"
        assert pkg.pri.verify_s1(staged).integrity.is_clean, "the candidate must be clean before intervention"
        targets = {
            "spec": staged / "migration-spec.json",
            "localized_data": next((staged / "data").rglob("*.csv")),
            "brief": staged / "migration-brief.md",
            "projection": staged / "data-access.json",
        }
        path = targets[change]
        path.write_bytes(path.read_bytes() + b"\n")
        mutations.append(change)

    def intervene(*args: object, **kwargs: object) -> object:
        if seam == "swap_entry":
            mutate()
        result = original(*args, **kwargs)
        if seam in ("assembly_return", "budget_return") or (seam == "retired_check" and args[1] == retired):
            mutate()
        return result

    def record_publication(source: Path, destination: Path) -> None:
        if source == staged and destination == final:
            publications.append(True)
        rename(source, destination)

    monkeypatch.setattr(pkg, seam_name, intervene)
    monkeypatch.setattr(pkg, "_rename_retrying", record_publication)
    with pytest.raises(pkg.PackagingError, match="^data_access_candidate_changed$"):
        pkg.package_unit(bundle, UNIT, out, **options)
    assert mutations == [change]
    assert not publications, "refusal must precede candidate exposure, not roll it back afterwards"
    assert _files(final) == prior, "all prior bytes, including the manifest, must survive"
    assert final.lstat().st_ino == prior_id, "restore the prior directory itself, not a reconstructed copy"
    assert not staged.exists() and not retired.exists()


@pytest.mark.parametrize("prior_exists", [False, True])
@pytest.mark.parametrize("discard_edits", [False, True])
def test_unchanged_candidate_check_is_the_last_step_before_publication(  # pylint: disable=too-many-locals
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_exists: bool, discard_edits: bool
) -> None:
    """New/replacement publication always checks once, including when prior-edit protection is waived."""
    bundle, out, options = _local_bundle(tmp_path)
    if prior_exists:
        pkg.package_unit(bundle, UNIT, out, **options)
    final = out / UNIT
    staged, retired = pkg.staging_dir(out, UNIT), pkg.retired_dir(final)
    prior = _files(final) if prior_exists else None
    events = []
    candidate = {}
    fits, swap, check, rename = (
        pkg.assert_assembled_fits,
        pkg.replace_dir,
        pkg._final_data_access_check,
        pkg._rename_retrying,
    )

    def budget(*args: object, **kwargs: object) -> None:
        fits(*args, **kwargs)
        events.append("budget")

    def enter_swap(*args: object, **kwargs: object) -> None:
        events.append("swap")
        swap(*args, **kwargs)

    def checked(*args: object, **kwargs: object) -> None:
        assert events == ["budget", "swap"], "the authoritative check must not precede producer reads or swap"
        assert not final.exists(), "the prior package must already be retired before candidate validation"
        assert retired.exists() == prior_exists
        if prior_exists:
            assert _files(retired) == prior
        check(*args, **kwargs)
        candidate.update(_files(staged))
        events.append("candidate")

    def publish(source: Path, destination: Path) -> None:
        if source == staged and destination == final:
            assert events[-1] == "candidate", "publication cannot omit the bound candidate callback"
            events.append("publish")
        rename(source, destination)

    monkeypatch.setattr(pkg, "assert_assembled_fits", budget)
    monkeypatch.setattr(pkg, "replace_dir", enter_swap)
    monkeypatch.setattr(pkg, "_final_data_access_check", checked)
    monkeypatch.setattr(pkg, "_rename_retrying", publish)
    result = pkg.package_unit(bundle, UNIT, out, **options, discard_edits=discard_edits)
    assert events == ["budget", "swap", "candidate", "publish"]
    assert _files(final) == candidate
    assert result["unit"] == UNIT and pkg.pri.verify_s1(final).integrity.is_clean
    assert pkg.data_access.read_data_access(final / "data-access.json").state == "local_import_ready"
    assert not staged.exists() and not retired.exists()


@pytest.mark.parametrize("prior_exists", [False, True])
def test_candidate_callback_exception_restores_without_exposure(  # pylint: disable=too-many-locals
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prior_exists: bool
) -> None:
    """A check that cannot complete must preserve the old package and propagate the original exception."""
    bundle, out, options = _local_bundle(tmp_path)
    if prior_exists:
        pkg.package_unit(bundle, UNIT, out, **options)
    final = out / UNIT
    staged, retired = pkg.staging_dir(out, UNIT), pkg.retired_dir(final)
    prior = _files(final) if prior_exists else None
    prior_id = final.lstat().st_ino if prior_exists else None
    checked, publications = [], []
    failure = RuntimeError("fixture-candidate-check-failed")
    rename = pkg._rename_retrying

    def cannot_check(*_args: object, **_kwargs: object) -> None:
        assert not final.exists() and retired.exists() == prior_exists
        checked.append(True)
        raise failure

    def publish(source: Path, destination: Path) -> None:
        if source == staged and destination == final:
            publications.append(True)
        rename(source, destination)

    monkeypatch.setattr(pkg, "_final_data_access_check", cannot_check)
    monkeypatch.setattr(pkg, "_rename_retrying", publish)
    with pytest.raises(RuntimeError, match="^fixture-candidate-check-failed$") as caught:
        pkg.package_unit(bundle, UNIT, out, **options)
    assert caught.value is failure
    assert checked == [True] and not publications
    if prior_exists:
        assert _files(final) == prior and final.lstat().st_ino == prior_id
    else:
        assert not final.exists()
    assert not staged.exists() and not retired.exists()


@pytest.mark.parametrize("prior_exists", [False, True])
@pytest.mark.parametrize("callback", ["omitted", "none"])
def test_swap_cannot_omit_candidate_validation(tmp_path: Path, prior_exists: bool, callback: str) -> None:
    """A caller must supply a real candidate callback; the prior-package callback is not a substitute."""
    staged, final = tmp_path / "staged", tmp_path / "Book"
    staged.mkdir()
    (staged / "candidate.txt").write_bytes(b"candidate")
    if prior_exists:
        final.mkdir()
        (final / "prior.txt").write_bytes(b"prior")
    prior, candidate = _files(final), _files(staged)
    callbacks: dict[str, Callable[[], None] | None] = {} if callback == "omitted" else {"verify_staged": None}
    with pytest.raises(TypeError):
        pkg.replace_dir(staged, final, **callbacks)
    assert _files(final) == prior and final.exists() == prior_exists
    assert _files(staged) == candidate
    assert not pkg.retired_dir(final).exists()
