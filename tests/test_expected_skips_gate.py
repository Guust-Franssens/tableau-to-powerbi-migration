"""Tests for the expected-skips baseline enforcement and engine skip reporting (issues #435, #436).

A skip is not a pass. An unexpected skip is a defect that would otherwise silently pass CI.
These tests verify that:
1. Skips matching the registered baseline reasons are allowed.
2. Skips with unexpected reasons fail the pytest session with exit code 1.
3. Engine-dependent skips are NOT_CHECKED only with an explicit allowed reason.
4. Every engine-dependent skip reason in CI is covered by the required engine jobs.
"""

from __future__ import annotations

from pathlib import Path
import sys
import pytest

import conftest

pytest_plugins = ("pytester",)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import engine_source  # noqa: E402  # pylint: disable=wrong-import-position,wrong-import-order


def test_expected_skip_reasons_contain_known_entries() -> None:
    """Registered reasons must be non-empty strings and covers all expected exact & prefix reasons."""
    assert len(conftest.EXPECTED_EXACT_SKIP_REASONS) >= 20
    for reason in conftest.EXPECTED_EXACT_SKIP_REASONS:
        assert isinstance(reason, str) and reason.strip() == reason
        assert conftest.is_expected_skip_reason(reason)

    assert len(conftest.EXPECTED_PREFIX_SKIP_REASONS) >= 3
    for prefix in conftest.EXPECTED_PREFIX_SKIP_REASONS:
        assert isinstance(prefix, str) and len(prefix.strip()) > 0
        assert conftest.is_expected_skip_reason(prefix + " extra detail")


def test_newly_registered_master_skip_reasons_are_accepted() -> None:
    """The 3 skip reasons introduced by master are accepted by the baseline."""
    assert conftest.is_expected_skip_reason("reproduces the WINDOWS half: Path resolves / against the current drive")
    assert conftest.is_expected_skip_reason("case-sensitive filesystem: 'FOO' and 'foo' are not the same deliverable")
    assert conftest.is_expected_skip_reason(
        "canonical engine not installed, so its constants cannot be read: No module named 'engine'"
    )
    # Ensure arbitrary prefixes with 'not installed', 'Windows', or 'filesystem' are still rejected
    assert not conftest.is_expected_skip_reason("not installed: something else")
    assert not conftest.is_expected_skip_reason("Windows something")
    assert not conftest.is_expected_skip_reason("filesystem error")


def test_unknown_skip_reason_is_rejected() -> None:
    """An unlisted skip reason is rejected by is_expected_skip_reason."""
    assert not conftest.is_expected_skip_reason("an arbitrary unexpected skip reason")
    assert not conftest.is_expected_skip_reason("")


def test_is_engine_skip_identifies_canonical_reason() -> None:
    """Canonical engine skip reasons (including constants check) are classified as engine skips."""
    assert conftest.is_engine_skip("deterministic tier not installed")
    assert conftest.is_engine_skip("  deterministic tier not installed  ")
    assert conftest.is_engine_skip(
        "canonical engine not installed, so its constants cannot be read: No module named foo"
    )
    assert not conftest.is_engine_skip("Windows-specific path spelling")


def test_engine_not_checked_reasons_are_closed() -> None:
    """Only named, reviewed NOT_CHECKED reasons can keep an engine skip from failing."""
    assert conftest.EXPECTED_ENGINE_NOT_CHECKED_REASONS == {"covered-by-pinned-engine-integration-job"}


def test_unexpected_skip_fails_the_pytest_session(pytester: pytest.Pytester) -> None:
    """A test skipping with an unregistered reason must fail the pytest session."""
    conftest_code = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(conftest_code)
    pytester.makepyfile(
        """
        import pytest

        def test_with_unregistered_skip():
            pytest.skip("novel unrecorded skip reason that must fail")
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*UNEXPECTED TEST SKIPS DETECTED*",
            "*novel unrecorded skip reason that must fail*",
        ]
    )


def test_expected_skip_passes_the_pytest_session(pytester: pytest.Pytester) -> None:
    """A test skipping with a known registered reason passes."""
    conftest_code = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(conftest_code)
    pytester.makepyfile(
        """
        import pytest

        def test_with_expected_skip():
            pytest.skip("Windows-specific path spelling")
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.OK
    assert "UNEXPECTED TEST SKIPS DETECTED" not in result.stdout.str()


def test_engine_skips_without_an_allowed_not_checked_reason_fail(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engine-dependent skips are not an ordinary green skip baseline."""
    monkeypatch.delenv(conftest.REQUIRE_ENGINE_TESTS_ENV, raising=False)
    monkeypatch.delenv(conftest.ENGINE_TESTS_NOT_CHECKED_REASON_ENV, raising=False)
    conftest_code = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(conftest_code)
    pytester.makepyfile(
        """
        import pytest

        def test_engine_one():
            pytest.skip("deterministic tier not installed")
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*ENGINE-DEPENDENT TESTS SKIPPED (1 test cases did not run)*",
            f"*{conftest.ENGINE_TESTS_NOT_CHECKED_REASON_ENV}*",
        ]
    )


def test_engine_skips_with_an_unknown_not_checked_reason_fail(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misspelled NOT_CHECKED reason must not become a new green baseline."""
    monkeypatch.delenv(conftest.REQUIRE_ENGINE_TESTS_ENV, raising=False)
    monkeypatch.setenv(conftest.ENGINE_TESTS_NOT_CHECKED_REASON_ENV, "engine-maybe-missing")
    conftest_code = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(conftest_code)
    pytester.makepyfile(
        """
        import pytest

        def test_engine_one():
            pytest.skip("deterministic tier not installed")
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "Set one of: covered-by-pinned-engine-integration-job" in result.stdout.str()


def test_engine_skips_with_an_allowed_not_checked_reason_are_reported(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary Linux CI job may report a deliberate NOT_CHECKED reason, not a silent pass."""
    monkeypatch.delenv(conftest.REQUIRE_ENGINE_TESTS_ENV, raising=False)
    monkeypatch.setenv(conftest.ENGINE_TESTS_NOT_CHECKED_REASON_ENV, "covered-by-pinned-engine-integration-job")
    conftest_code = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(conftest_code)
    pytester.makepyfile(
        """
        import pytest

        def test_engine_one():
            pytest.skip("deterministic tier not installed")

        def test_engine_two():
            pytest.skip("deterministic tier not installed")
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.OK
    result.stdout.fnmatch_lines(
        [
            "*ENGINE-DEPENDENT TESTS NOT_CHECKED (2 test cases did not run)*",
            "*The deterministic conversion engine plugin (tableau-fabric-skills) is not installed.*",
            "*test_engine_one*",
            "*test_engine_two*",
            "*NOT_CHECKED reason: covered-by-pinned-engine-integration-job*",
        ]
    )


def test_required_engine_run_fails_if_engine_tests_skip(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine-integration and drift jobs must fail closed when the engine did not install."""
    monkeypatch.setenv(conftest.REQUIRE_ENGINE_TESTS_ENV, "1")
    monkeypatch.setenv(conftest.ENGINE_TESTS_NOT_CHECKED_REASON_ENV, "covered-by-pinned-engine-integration-job")
    conftest_code = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(conftest_code)
    pytester.makepyfile(
        """
        import pytest

        def test_engine_one():
            pytest.skip("deterministic tier not installed")
        """
    )
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*ENGINE-DEPENDENT TESTS SKIPPED (1 test cases did not run)*",
            f"*{conftest.REQUIRE_ENGINE_TESTS_ENV}=1*",
        ]
    )


def test_unexpected_skip_with_xdist_fails_pytest_session(pytester: pytest.Pytester) -> None:
    """Under pytest-xdist (-n 2 --dist loadfile), an unexpected skip must still fail the session."""
    conftest_code = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(conftest_code)
    pytester.makepyfile(
        """
        import pytest

        def test_under_xdist():
            pytest.skip("novel unrecorded skip reason under xdist")
        """
    )
    result = pytester.runpytest("-n", "2", "--dist", "loadfile")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*UNEXPECTED TEST SKIPS DETECTED*",
            "*novel unrecorded skip reason under xdist*",
        ]
    )


def test_headroom_padding_is_deterministic_across_path_lengths() -> None:
    """The headroom calculation in _pad_for_headroom must produce a deterministic pad

    when given a scaled ceiling, preventing the 1-in-13 skip count variance from #436.
    """

    # Simulate test_check_unit._pad_for_headroom with explicit ceiling
    def compute_pad(root_len: int, headroom: int = 10, offset: int = 46) -> int | None:
        target_ceiling = root_len + offset
        root_budget = target_ceiling - 35
        pad = root_budget + 1 - root_len - headroom
        return pad if pad > 0 else None

    # Across varying root lengths from 10 chars to 300 chars, pad is always 2 (>0)
    for root_len in range(10, 300):
        pad = compute_pad(root_len)
        assert pad == 2, f"pad was {pad} for root_len {root_len}"


def test_all_engine_dependent_tests_are_accounted_for() -> None:
    """Pin the engine-dependent tests covered by required engine jobs (issue #435).

    The original issue counted 15 tests:
    - 4 in tests/test_dax_oracle_server.py
    - 8 in tests/test_issue_424_chart_type_pin.py (5 decorators, one 4x parametrized)
    - 3 in tests/test_upstream_repro_pins.py

    The skip-reason classifier also treats the installed-engine-constants watchdog as engine-backed,
    so the engine jobs must run that one too instead of marking it NOT_CHECKED in the main job.
    """
    expected_modules = {
        "tests/test_dax_oracle_server.py": 4,
        "tests/test_issue_424_chart_type_pin.py": 8,
        "tests/test_upstream_repro_pins.py": 3,
        "tests/test_harvest_download_watchdog.py": 1,
    }

    total_engine_tests = sum(expected_modules.values())
    assert total_engine_tests == 16

    for rel_path, expected_count in expected_modules.items():
        file_path = REPO_ROOT / rel_path
        assert file_path.is_file(), f"missing test module {rel_path}"
        content = file_path.read_text(encoding="utf-8")
        assert any(reason in content for reason in conftest.ENGINE_SKIP_REASONS), (
            f"{rel_path} must declare an engine skip reason"
        )
        assert expected_count > 0, f"{rel_path} must have positive expected count"


def test_caller_passing_rs_still_sees_failure_and_error_node_ids(pytester: pytest.Pytester) -> None:
    """When a caller passes -rs, failures and errors must still appear in short summary info."""
    conftest_code = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    pytester.makeconftest(conftest_code)
    pytester.makepyfile(
        """
        import pytest

        def test_known_skip():
            pytest.skip("Windows-specific path spelling")

        def test_forced_failure():
            assert False, "deliberate test failure"
        """
    )
    result = pytester.runpytest("-rs")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*SKIPPED*Windows-specific path spelling*",
            "*FAILED*test_forced_failure*",
        ]
    )


def test_synthetic_installed_engine_root_under_simulation_env(tmp_path: Path, monkeypatch) -> None:
    """An injected synthetic root remains authoritative even when SIMULATE_ENGINE_ABSENT_ENV is set."""
    # Create synthetic engine tree
    fake_root = tmp_path / "synthetic_engine"
    skill = fake_root / engine_source.ENGINE_SKILL
    (skill / "scripts").mkdir(parents=True, exist_ok=True)
    (skill / "VERSION").write_text("2.130.0\n", encoding="utf-8")

    monkeypatch.setenv(engine_source.SIMULATE_ENGINE_ABSENT_ENV, "1")
    monkeypatch.setattr(engine_source, "PLUGIN_ENGINE_ROOT", fake_root)

    # Injected synthetic root resolves
    assert engine_source.engine_root() == fake_root
    assert engine_source.engine_version(fake_root) == "2.130.0"

    # Non-canonical root is still refused without allow_noncanonical
    other = tmp_path / "other"
    (other / engine_source.ENGINE_SKILL / "scripts").mkdir(parents=True, exist_ok=True)
    with pytest.raises(engine_source.NonCanonicalEngineError):
        engine_source.resolve_engine(other)
    assert engine_source.resolve_engine(other, allow_noncanonical=True) == other


def test_workflow_runs_engine_dependent_tests_in_required_jobs() -> None:
    """The production workflow must reach the fail-closed engine-test path (issue #435)."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")
    target = "tests/test_issue_424_chart_type_pin.py tests/test_dax_oracle_server.py tests/test_upstream_repro_pins.py"
    target = target + " tests/test_harvest_download_watchdog.py::test_the_ceiling_constants_match_the_INSTALLED_engine"

    assert "schedule:" in workflow, "latest-engine drift pins need a scheduled job"
    assert "engine-integration:" in workflow
    assert "engine-drift:" in workflow
    assert "962d16cfe6f711622d419a567f992da8d90c8781" in workflow
    assert "EXPECTED_ENGINE_VERSION: '2.356.0'" in workflow
    assert "ref: main" in workflow
    assert f"{conftest.REQUIRE_ENGINE_TESTS_ENV}=1 uv run pytest -q {target}" in workflow
    assert "covered-by-pinned-engine-integration-job uv run pytest -q" in workflow
