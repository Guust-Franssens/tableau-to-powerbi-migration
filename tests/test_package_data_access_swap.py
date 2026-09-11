"""PR #608: the held candidate is checked inside the final guarded publication, never before it."""

from __future__ import annotations

# These controls intentionally exercise the producer's private assembly and publication seams.
# pylint: disable=protected-access

from collections.abc import Callable
from pathlib import Path

import pytest

from test_package_data_access_snapshot import _files, _local_bundle
from test_package_unit_gates import UNIT, pkg


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
