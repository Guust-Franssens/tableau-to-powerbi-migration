"""Direct exact-root controls for #558, independent of the host filesystem's case behavior."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from test_package_source import (
    _ForbiddenSourceOperation,
    authority,
    corpus,
    filesystem,
    forbid_source_operations,
    project_without_io,
    readiness,
    roles,
    source,
)

UPPER = Path("packages") / "Unit"
LOWER = Path("packages") / "unit"
OTHER = Path("packages") / "Other"


def forbidden(*_args: object, **_kwargs: object) -> object:
    """A named assertion, caught inside global patches rather than a pytest infrastructure error."""
    raise _ForbiddenSourceOperation("invalid root binding reached an authority or downstream reader")


def test_exact_root_guard_is_case_sensitive_even_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    value = authority(UPPER).source_handoff()
    assert source.exact_root_matches(UPPER, str(UPPER))
    assert not source.exact_root_matches(LOWER, str(UPPER))
    assert not source.exact_root_matches(UPPER, str(LOWER))
    result = project_without_io(monkeypatch, replace(value, package_root=LOWER))
    assert result.codes == ("source_handoff_invalid",)
    assert result.path is None


@pytest.mark.parametrize("observed, target", [(UPPER, LOWER), (LOWER, UPPER)])
def test_s1_observation_guard_rejects_a_case_foreign_identity_without_a_later_guard(
    observed: Path, target: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleared = authority(observed).verified
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        own = cleared.is_bound_to(str(observed))
        foreign = cleared.is_bound_to(str(target))

    assert own is True
    assert foreign is False, "the S1 observation guard itself must reject a case-foreign identity"


@pytest.mark.parametrize("roots", [(UPPER, LOWER), (LOWER, UPPER), (UPPER, UPPER)])
def test_duplicate_or_case_colliding_targets_stop_before_s1_s2_or_discovery(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    classification = authority().verified.classification
    violation, reports = None, []
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        patch.setattr(readiness, "verify_package", forbidden)
        patch.setattr(readiness, "verify_phase1_role_identity", forbidden)
        patch.setattr(readiness, "_scan_safe_target", forbidden)
        try:
            checked = readiness._precheck_cohort(list(roots), [classification, classification])
            reports = [readiness.scan(root, prechecked=entry) for root, entry in zip(roots, checked, strict=True)]
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert len(reports) == 2
    for report in reports:
        assert report["status"] == "CANNOT_ESTABLISH"
        assert report["package_source"][0]["codes"] == ["package_root_binding_invalid"]
        assert report["pages_expected"] == report["evidence_records"] == 0


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
def test_s2_map_keys_are_exact_lexical_identities(reverse: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    first, second = authority(UPPER), authority(OTHER)
    results = (second, first) if reverse else (first, second)
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        indexed = source.bind_root_results(
            [str(UPPER), str(OTHER)], results, lambda result: roles.verified_root_binding(result.verified)
        )

    assert indexed is not None
    assert set(indexed) == {str(UPPER), str(OTHER)}
    assert all(type(key) is str for key in indexed)
    assert indexed[str(UPPER)] is first
    assert indexed[str(OTHER)] is second
    assert str(LOWER) not in indexed, "a Windows case alias cannot retrieve another target's authority"


def malformed_results(change: str) -> tuple[roles.Phase1RoleIdentityResult, ...]:
    """Literal authority transformations, never fixtures blessed by the package writer."""
    first, second = authority(UPPER), authority(OTHER)
    if change == "missing":
        return (first,)
    if change == "empty":
        return ()
    if change == "extra":
        return first, second, authority(Path("packages") / "Extra")
    if change == "duplicate-overwrite":
        return first, first
    if change == "missing-binding":
        return replace(first, verified=None), second
    if change == "root-spelling":
        return replace(first, verified=replace(first.verified, root=LOWER)), second
    if change == "identity-spelling":
        return replace(first, verified=replace(first.verified, root_identity=str(LOWER))), second
    if change == "case-foreign":
        return authority(LOWER), second
    raise AssertionError(f"unknown test change: {change}")


BINDING_CHANGES = (
    "missing",
    "empty",
    "extra",
    "duplicate-overwrite",
    "missing-binding",
    "root-spelling",
    "identity-spelling",
    "case-foreign",
)


@pytest.mark.parametrize("change", BINDING_CHANGES)
def test_s2_map_refuses_every_non_bijection(change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    results = malformed_results(change)
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        indexed = source.bind_root_results(
            [str(UPPER), str(OTHER)], results, lambda result: roles.verified_root_binding(result.verified)
        )

    assert indexed is None, "missing/extra/duplicate/mismatched results must not become a partial or overwriting map"


@pytest.mark.parametrize("change", BINDING_CHANGES)
@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reversed"])
def test_consumer_refuses_the_whole_invalid_result_map_before_reading_any_target(
    change: str, reverse: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = [OTHER, UPPER] if reverse else [UPPER, OTHER]
    classification = authority().verified.classification
    results = malformed_results(change)
    violation, reports = None, []
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        patch.setattr(readiness, "verify_package", lambda *_args: filesystem.PackageFilesystemResult("clean"))
        patch.setattr(readiness, "verify_phase1_role_identity", lambda *_args, **_kwargs: results)
        patch.setattr(readiness, "_scan_safe_target", forbidden)
        try:
            checked = readiness._precheck_cohort(roots, [classification, classification])
            reports = [readiness.scan(root, prechecked=entry) for root, entry in zip(roots, checked, strict=True)]
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert len(reports) == 2
    assert all(report["status"] == "CANNOT_ESTABLISH" for report in reports)
    assert all(report["package_source"][0]["codes"] == ["package_root_binding_invalid"] for report in reports)
    assert all(report["pages_expected"] == report["evidence_records"] == 0 for report in reports)


@pytest.mark.parametrize("change", ["precheck-root", "precheck-identity", "s2-root", "s2-identity", "s2-case-foreign"])
@pytest.mark.parametrize("package", [False, True], ids=["ordinary", "package"])
def test_each_scan_guard_uses_the_original_exact_root_without_rechecking(
    change: str, package: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = authority(UPPER)
    classification = (
        result.verified.classification
        if package
        else corpus.TargetClassification("ordinary", "ordinary_target", "", "ancestor", "Unit")
    )
    checked = readiness._Prechecked(UPPER, str(UPPER), classification, result.verified.integrity, result)
    if change == "precheck-root":
        checked = replace(checked, root=LOWER)
    elif change == "precheck-identity":
        checked = replace(checked, root_identity=str(LOWER))
    elif change == "s2-root":
        checked = replace(checked, roles=replace(result, verified=replace(result.verified, root=LOWER)))
    elif change == "s2-identity":
        checked = replace(checked, roles=replace(result, verified=replace(result.verified, root_identity=str(LOWER))))
    else:
        checked = replace(checked, roles=authority(LOWER))

    violation, report = None, None
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        patch.setattr(readiness, "_precheck", forbidden)
        patch.setattr(readiness, "_scan_safe_target", forbidden)
        try:
            report = readiness.scan(UPPER, prechecked=checked)
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert report is not None and report["status"] == "CANNOT_ESTABLISH"
    assert report["package_source"][0]["codes"] == ["package_root_binding_invalid"]


@pytest.mark.parametrize("reverse", [False, True])
def test_a_foreign_precheck_is_a_refusal_not_a_request_to_reclassify(
    reverse: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, observed = (LOWER, UPPER) if reverse else (UPPER, LOWER)
    result = authority(observed)
    checked = readiness._Prechecked(
        observed, str(observed), result.verified.classification, result.verified.integrity, result
    )
    violation, report = None, None
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        patch.setattr(readiness, "_precheck", forbidden)
        patch.setattr(readiness, "_scan_safe_target", forbidden)
        try:
            report = readiness.scan(target, prechecked=checked)
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert report is not None and report["status"] == "CANNOT_ESTABLISH"
    assert report["package_source"][0]["codes"] == ["package_root_binding_invalid"]


@pytest.mark.parametrize("reverse", [False, True])
def test_source_handoff_guard_requires_the_same_exact_target_identity(
    reverse: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, foreign = (LOWER, UPPER) if reverse else (UPPER, LOWER)
    violation, report = None, None
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        patch.setattr(readiness, "_scan_safe_target", forbidden)
        try:
            report = readiness._scan_verified_package(target, str(target), authority(foreign), False)
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert report is not None and report["status"] == "CANNOT_ESTABLISH"
    assert report["package_source"][0]["codes"] == ["package_root_binding_invalid"]


@pytest.mark.parametrize(
    "changes",
    [
        {"asset_path": "assets/CON.twb"},
        {"asset_path": "assets/./opaque.twb"},
        {"asset_path": "assets//opaque.twb"},
        {"codes": False},
        {"codes": []},
    ],
)
def test_malformed_ready_handoff_stops_the_consumer_before_any_source_or_evidence_read(
    changes: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    result = authority(UPPER)
    value = replace(result.source_handoff(), **changes)
    violation, report = None, None
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        patch.setattr(roles.Phase1RoleIdentityResult, "source_handoff", lambda _self: value)
        patch.setattr(readiness, "_scan_safe_target", forbidden)
        try:
            report = readiness._scan_verified_package(UPPER, str(UPPER), result, False)
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert report is not None and report["status"] == "CANNOT_ESTABLISH"
    assert report["package_source"][0]["codes"] == ["source_handoff_invalid"]
    assert report["pages_expected"] == report["evidence_records"] == 0


@pytest.mark.parametrize("roots", [(UPPER, LOWER), (LOWER, UPPER), (UPPER, UPPER)])
def test_s2_duplicate_or_case_colliding_roots_never_enter_s1(
    roots: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    violation, results = None, ()
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        patch.setattr(roles, "verify_s1", forbidden)
        patch.setattr(roles, "_facts", forbidden)
        try:
            results = roles.verify_phase1_role_identity(roots)
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert len(results) == 2
    assert all(
        result.verdict == "BLOCKED" and result.blockers == ("package_root_binding_invalid",) for result in results
    )


@pytest.mark.parametrize("change", [change for change in BINDING_CHANGES if change != "missing-binding"])
def test_s2_supplied_s1_observations_must_be_an_exact_bijection_before_reverification(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = [result.verified for result in malformed_results(change)]
    violation, results = None, ()
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        patch.setattr(roles, "verify_s1", forbidden)
        patch.setattr(roles, "_facts", forbidden)
        try:
            results = roles.verify_phase1_role_identity([UPPER, OTHER], verified=observed)
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert len(results) == 2
    assert all(result.blockers == ("package_root_binding_invalid",) for result in results)


@pytest.mark.parametrize("change", ["root-spelling", "identity-spelling", "case-foreign", "duplicate-overwrite"])
def test_s2_fresh_s1_observations_are_bound_before_any_role_read(change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    observed = iter(result.verified for result in malformed_results(change))
    violation, results = None, ()
    with monkeypatch.context() as patch:
        forbid_source_operations(patch)
        patch.setattr(roles, "verify_s1", lambda _root: next(observed))
        patch.setattr(roles, "_facts", forbidden)
        try:
            results = roles.verify_phase1_role_identity([UPPER, OTHER])
        except _ForbiddenSourceOperation as exc:
            violation = str(exc)

    assert violation is None, violation
    assert len(results) == 2
    assert all(result.blockers == ("package_root_binding_invalid",) for result in results)
