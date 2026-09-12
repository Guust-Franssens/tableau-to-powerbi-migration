"""PR #608: the held candidate is checked inside the final guarded publication, never before it."""

from __future__ import annotations

# These controls intentionally exercise the producer's private assembly and publication seams.
# pylint: disable=protected-access

from collections.abc import Callable
from dataclasses import replace
import json
from pathlib import Path

import pytest

from test_package_data_access_snapshot import _files, _local_bundle
from test_package_unit_gates import UNIT, _binding_cli, _binding_package, pkg


def test_binding_final_inspection_is_inside_swap_before_retired_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _binding_package(tmp_path)
    staged, retired = pkg.staging_dir(root.parent, root.name), pkg.retired_dir(root)
    events = []
    rename, final, cleanup = pkg._rename_retrying, pkg._binding_final, pkg._discard_scratch

    def moving(source, destination):
        rename(source, destination)
        if source == staged and destination == root:
            events.append("rename")

    def inspecting(*args, **kwargs):
        assert events == ["rename"] and retired.is_dir()
        inspection = final(*args, **kwargs)
        assert pkg.pri.verify_s1(root).integrity.is_clean
        events.append("final-s1-exact-inspection-cohort")
        return inspection

    def cleaning(path):
        if path == retired:
            assert events == ["rename", "final-s1-exact-inspection-cohort"]
            events.append("cleanup")
        return cleanup(path)

    monkeypatch.setattr(pkg, "_rename_retrying", moving)
    monkeypatch.setattr(pkg, "_binding_final", inspecting)
    monkeypatch.setattr(pkg, "_discard_scratch", cleaning)
    result = _binding_cli(root)
    assert result["exit_code"] == 0 and result["outcome"] == "published"
    assert result["inspection"]["state"] == "BOUND" and result["inspection"]["validation"] == "UNVALIDATED"
    assert events == ["rename", "final-s1-exact-inspection-cohort", "cleanup"]


@pytest.mark.parametrize(
    "failure", ["retire", "publish", "final", "rollback", "cleanup", "interrupt", "cleanup-interrupt"]
)
def test_binding_publication_failure_reports_exact_directory_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    root = _binding_package(tmp_path)
    before, original_id = _files(root), root.lstat().st_ino
    staged, retired = pkg.staging_dir(root.parent, root.name), pkg.retired_dir(root)
    rename, final, cleanup = pkg._rename_retrying, pkg._binding_final, pkg._discard_scratch
    hits = []

    def moving(source, destination):
        point = (
            "retire"
            if source == root and destination == retired
            else ("publish" if source == staged and destination == root else "rollback")
        )
        if failure == point or (failure == "rollback" and point == "publish"):
            hits.append(point)
            raise OSError("private-rename-canary")
        rename(source, destination)

    def inspecting(*args, **kwargs):
        assert retired.is_dir() and root.is_dir()
        if failure in ("final", "interrupt"):
            hits.append("final")
            if failure == "interrupt":
                raise KeyboardInterrupt()
            raise pkg._BindingRefusal("binding_final_inspection_failed", 1)
        return final(*args, **kwargs)

    def cleaning(path):
        if path == retired and failure in ("cleanup", "cleanup-interrupt"):
            hits.append("cleanup")
            if failure == "cleanup-interrupt":
                raise KeyboardInterrupt()
            return "private-cleanup-canary"
        return cleanup(path)

    monkeypatch.setattr(pkg, "_rename_retrying", moving)
    monkeypatch.setattr(pkg, "_binding_final", inspecting)
    monkeypatch.setattr(pkg, "_discard_scratch", cleaning)
    result = _binding_cli(root)
    expected = {
        "retire": ("unchanged", 3, ["retire"]),
        "publish": ("rolled-back", 3, ["publish"]),
        "final": ("rolled-back", 1, ["final"]),
        "rollback": ("cannot-establish", 3, ["publish", "rollback"]),
        "cleanup": ("published-with-residue", 1, ["cleanup"]),
        "interrupt": ("rolled-back", 130, ["final"]),
        "cleanup-interrupt": ("published-with-residue", 130, ["cleanup"]),
    }
    assert (result["outcome"], result["exit_code"], hits) == expected[failure]
    assert len(list(root.parent.rglob(pkg.MANIFEST_NAME))) <= 1
    assert not staged.exists()
    if failure == "rollback":
        assert not root.exists() and _files(retired) == before
    elif failure.startswith("cleanup"):
        assert _files(retired) == {key: raw for key, raw in before.items() if key != pkg.MANIFEST_NAME}
        assert root.lstat().st_ino != original_id and pkg.pri.verify_s1(root).integrity.is_clean
        assert result["inspection"]["state"] == "BOUND"
    else:
        assert _files(root) == before and root.lstat().st_ino == original_id
        assert not retired.exists()


@pytest.mark.parametrize("fault", ["s1", "exact-candidate", "inspection", "sanitize-placeholder"])
def test_binding_final_authorities_reject_at_the_final_root_not_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    root = _binding_package(tmp_path)
    if fault == "sanitize-placeholder":
        assert _binding_cli(root)["exit_code"] == 0
    before = _files(root)
    staged, retired = pkg.staging_dir(root.parent, root.name), pkg.retired_dir(root)
    hits = []
    if fault == "s1":
        verify = pkg.pri.verify_s1

        def unavailable(location):
            verified = verify(location)
            if location == root and retired.exists():
                assert pkg._package_directory_id(root) != pkg._package_directory_id(retired)
                hits.append(True)
                return replace(verified, integrity=replace(verified.integrity, status="unassessable"))
            return verified

        monkeypatch.setattr(pkg.pri, "verify_s1", unavailable)
    elif fault == "exact-candidate":
        rename = pkg._rename_retrying

        def changed(source, destination):
            rename(source, destination)
            if source == staged and destination == root:
                path = root / "migration-spec.json"
                path.write_bytes(path.read_bytes() + b"\n")
                manifest = json.loads((root / "package-manifest.json").read_bytes())
                manifest["contents"]["files"] = pkg.package_contents(root)
                (root / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                assert pkg.pri.verify_s1(root).integrity.is_clean
                hits.append(True)

        monkeypatch.setattr(pkg, "_rename_retrying", changed)
    else:
        inspect = pkg._binding_observation

        def mismatch(package, location):
            result = inspect(package, location)
            if location == root and retired.exists():
                assert not staged.exists(), "the candidate must already have its final address"
                hits.append(True)
                if fault == "inspection":
                    assert result["codes"] == []
                    result["codes"] = ["binding_not_current"]
                else:
                    assert result["parameters"][0]["placeholder"]
                    result["parameters"][0]["placeholder"] = False
            return result

        monkeypatch.setattr(pkg, "_binding_observation", mismatch)
    result = _binding_cli(root, *(["--sanitize"] if fault == "sanitize-placeholder" else []))
    assert hits == [True]
    assert result["outcome"] == "rolled-back"
    expected = {
        "s1": (3, "binding_s1_not_clean"),
        "exact-candidate": (3, "binding_final_candidate_changed"),
        "inspection": (1, "binding_final_inspection_failed"),
        "sanitize-placeholder": (3, "binding_sanitize_incomplete"),
    }
    assert (result["exit_code"], result["codes"]) == (expected[fault][0], [expected[fault][1]])
    assert _files(root) == before and not staged.exists() and not retired.exists()
    assert len(list(root.parent.rglob(pkg.MANIFEST_NAME))) == 1


@pytest.mark.parametrize("barrier", ["marker", "malformed", "acl", "query-failure"])
@pytest.mark.parametrize("address", ["original", "retired"])
def test_binding_checks_the_retired_original_physical_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, barrier: str, address: str
) -> None:
    from test_credential_gate import _physical_marker  # pylint: disable=import-outside-toplevel

    root = _binding_package(tmp_path)
    retired = pkg.retired_dir(root)
    stage = pkg._binding_stage
    observed = []
    retained = {}

    def write_marker():
        if barrier in ("marker", "malformed"):
            (root / pkg.data_access.MARKER).write_text(
                json.dumps(_physical_marker() if barrier == "marker" else {}), encoding="utf-8"
            )

    if address == "original":
        write_marker()
        manifest = json.loads((root / "package-manifest.json").read_bytes())
        manifest["contents"]["files"] = pkg.package_contents(root)
        (root / "package-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        retained.update(_files(root))

    def after_stage(*args, **kwargs):
        assert address == "retired", "the original physical stop must refuse before staging"
        identity = stage(*args, **kwargs)
        write_marker()
        retained.update(_files(root))
        return identity

    def query(args):
        if args == [str((root if address == "original" else retired) / "fabric")]:
            observed.append(True)
            return (5, "private-acl-canary") if barrier == "query-failure" else (0, "fixture:(DENY)(WD)")
        return 0, "fixture:(F)"

    inspect = pkg.data_access.inspect_physical_barrier

    def observe(location):
        result = inspect(location)
        if location == (root if address == "original" else retired) and barrier in ("marker", "malformed"):
            observed.append(True)
        return result

    monkeypatch.setattr(pkg, "_binding_stage", after_stage)
    monkeypatch.setattr(pkg.data_access.platform, "system", lambda: "Windows")
    monkeypatch.setattr(pkg.data_access, "_icacls", query)
    monkeypatch.setattr(pkg.data_access, "inspect_physical_barrier", observe)
    result = _binding_cli(root)
    expected = {
        "marker": (1, "physical_marker_blocked"),
        "malformed": (3, "physical_marker_invalid"),
        "acl": (1, "physical_acl_blocked"),
        "query-failure": (3, "physical_acl_query_failed"),
    }
    assert observed == [True]
    assert result["outcome"] == ("rolled-back" if address == "retired" else "unchanged")
    assert (result["exit_code"], result["codes"]) == (expected[barrier][0], [expected[barrier][1]])
    assert _files(root) == retained and not retired.exists()


@pytest.mark.parametrize("seam", ["stage", "post-retire", "post-publish", "post-swap", "rollback"])
def test_binding_interrupts_never_report_an_unknown_publication_as_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seam: str
) -> None:
    root = _binding_package(tmp_path)
    before = _files(root)
    staged, retired = pkg.staging_dir(root.parent, root.name), pkg.retired_dir(root)
    hits = []
    rename, stage, swap = pkg._rename_retrying, pkg._binding_stage, pkg.replace_dir

    def moving(source, destination):
        if seam == "rollback" and source == staged and destination == root:
            raise OSError("force rollback")
        if seam == "rollback" and source == retired:
            hits.append(True)
            raise KeyboardInterrupt()
        rename(source, destination)
        if (seam == "post-retire" and source == root and destination == retired) or (
            seam == "post-publish" and source == staged and destination == root
        ):
            hits.append(True)
            raise KeyboardInterrupt()

    def staging(*args, **kwargs):
        result = stage(*args, **kwargs)
        if seam == "stage":
            hits.append(True)
            raise KeyboardInterrupt()
        return result

    def swapping(*args, **kwargs):
        result = swap(*args, **kwargs)
        if seam == "post-swap":
            hits.append(True)
            raise KeyboardInterrupt()
        return result

    monkeypatch.setattr(pkg, "_rename_retrying", moving)
    monkeypatch.setattr(pkg, "_binding_stage", staging)
    monkeypatch.setattr(pkg, "replace_dir", swapping)
    result = _binding_cli(root)
    assert hits == [True]
    assert result["exit_code"] == 130
    assert len(list(root.parent.rglob(pkg.MANIFEST_NAME))) <= 1
    assert (
        result["outcome"]
        == {
            "stage": "unchanged",
            "post-retire": "rolled-back",
            "post-publish": "rolled-back",
            "post-swap": "published",
            "rollback": "cannot-establish",
        }[seam]
    )
    assert not staged.exists()
    if seam == "rollback":
        assert not root.exists() and _files(retired) == before
    elif seam == "post-swap":
        assert pkg.pri.verify_s1(root).integrity.is_clean and not retired.exists()
        assert _binding_cli(root, "--inspect")["exit_code"] == 0
    else:
        assert _files(root) == before and not retired.exists()


@pytest.mark.parametrize(
    "failure",
    [
        "staging-cleanup",
        "rollback-candidate",
        "rollback-original",
        "unknown-original",
        "staged-marker-unlink",
        "retired-marker-unlink",
        "retired-marker-interrupt",
        "retired-marker-hide",
    ],
)
def test_binding_cleanup_and_obstructed_recovery_close_package_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """The filesystem's surviving markers, not the returned outcome, are the independent oracle."""
    root = _binding_package(tmp_path)
    before = _files(root)
    staged, retired = pkg.staging_dir(root.parent, root.name), pkg.retired_dir(root)
    stage, final = pkg._binding_stage, pkg._binding_final
    rename, cleanup, directory_id, unlink = (
        pkg._rename_retrying,
        pkg._discard_scratch,
        pkg._package_directory_id,
        Path.unlink,
    )
    hits = []
    refused = []
    stage_failure = failure in ("staging-cleanup", "staged-marker-unlink")

    def staging(*args, **kwargs):
        identity = stage(*args, **kwargs)
        if stage_failure:
            hits.append("stage")
            raise pkg._BindingRefusal("binding_candidate_changed")
        return identity

    def inspecting(*args, **kwargs):
        if failure.startswith("rollback") or failure == "unknown-original":
            refused.append(True)
            raise pkg._BindingRefusal("binding_final_inspection_failed", 1)
        return final(*args, **kwargs)

    def moving(source, destination):
        obstructed = (
            (failure == "rollback-candidate" and source == root and destination == staged)
            or (failure == "rollback-original" and source == retired and destination == root)
            or (failure == "retired-marker-hide" and source == retired / pkg.MANIFEST_NAME)
        )
        if obstructed:
            hits.append("rename")
            raise OSError("private-rename-canary")
        return rename(source, destination)

    def identity(location):
        result = directory_id(location)
        if failure == "unknown-original" and refused and location == retired:
            hits.append("identity")
            return result[0], result[1] + 1
        return result

    def cleaning(location):
        if (location == staged and (stage_failure or failure == "rollback-original")) or (
            location == retired and failure.startswith("retired-marker")
        ):
            hits.append("cleanup")
            return "private-cleanup-canary"
        return cleanup(location)

    def removing(path, *args, **kwargs):
        target = staged if failure == "staged-marker-unlink" else retired
        if "marker" in failure and path == target / pkg.MANIFEST_NAME:
            hits.append("unlink")
            if failure == "retired-marker-interrupt":
                raise KeyboardInterrupt()
            raise PermissionError("private-marker-canary")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(pkg, "_binding_stage", staging)
    monkeypatch.setattr(pkg, "_binding_final", inspecting)
    monkeypatch.setattr(pkg, "_rename_retrying", moving)
    monkeypatch.setattr(pkg, "_package_directory_id", identity)
    monkeypatch.setattr(pkg, "_discard_scratch", cleaning)
    monkeypatch.setattr(Path, "unlink", removing)
    result = _binding_cli(root)
    assert hits and result["exit_code"] != 0
    assert "private-" not in json.dumps(result)
    discovered = set(root.parent.rglob(pkg.MANIFEST_NAME))
    assert len(discovered) <= 1, "a failed transaction exposed more than one package"
    if failure.startswith("rollback"):
        assert discovered == {retired / pkg.MANIFEST_NAME}
        assert _files(retired) == before
        assert (result["outcome"], result["exit_code"], result["codes"]) == (
            "cannot-establish",
            3,
            ["binding_rollback_failed"],
        )
        assert result["inspection"] == {}
    elif failure == "unknown-original":
        assert discovered == set()
        assert (result["outcome"], result["exit_code"], result["codes"]) == (
            "cannot-establish",
            3,
            ["binding_discovery_unassessable"],
        )
        assert _files(retired) == {key: raw for key, raw in before.items() if key != pkg.MANIFEST_NAME}
        assert (root / "fabric").is_dir(), "uncertain recovery evidence must not be deleted"
    elif stage_failure:
        assert discovered == {root / pkg.MANIFEST_NAME} and _files(root) == before
        assert (result["outcome"], result["exit_code"]) == ("unchanged", 3)
    elif failure == "retired-marker-hide":
        assert discovered == {retired / pkg.MANIFEST_NAME} and _files(retired) == before
        assert (result["outcome"], result["exit_code"], result["codes"]) == (
            "cannot-establish",
            3,
            ["binding_discovery_unassessable"],
        )
        assert result["inspection"] == {}
    else:
        assert discovered == {root / pkg.MANIFEST_NAME}
        assert result["outcome"] == "published-with-residue"
        assert result["exit_code"] == (130 if failure == "retired-marker-interrupt" else 1)
        assert result["inspection"]["state"] == "BOUND"
        assert (retired / f".{pkg.MANIFEST_NAME}.retired").read_bytes() == before[pkg.MANIFEST_NAME]


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
