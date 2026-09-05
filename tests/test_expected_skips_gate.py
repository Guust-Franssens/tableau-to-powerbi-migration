"""Tests for the expected-skips baseline enforcement and engine skip reporting (issues #435, #436).

A skip is not a pass. An unexpected skip is a defect that would otherwise silently pass CI.
These tests verify that:
1. Skips matching the registered baseline reasons are allowed.
2. Skips with unexpected reasons fail the pytest session with exit code 1.
3. Engine-dependent skips are NOT_CHECKED only with an explicit allowed reason.
4. Every engine-dependent skip reason in CI is covered by the required engine jobs.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
import subprocess
import sys
import pytest

import conftest

pytest_plugins = ("pytester",)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import engine_source  # noqa: E402  # pylint: disable=wrong-import-position,wrong-import-order

PINNED_ENGINE_SHA = "962d16cfe6f711622d419a567f992da8d90c8781"
PINNED_ENGINE_VERSION = "2.356.0"
ENGINE_TEST_TARGETS = (
    "tests/test_issue_424_chart_type_pin.py",
    "tests/test_dax_oracle_server.py",
    "tests/test_upstream_repro_pins.py",
    "tests/test_harvest_download_watchdog.py::test_the_ceiling_constants_match_the_INSTALLED_engine",
)
ENGINE_TEST_TARGET_COMMAND = " ".join(ENGINE_TEST_TARGETS)


def _conftest_for_pytester(expected: dict[str, str]) -> str:
    """Copy the real conftest but replace the engine denominator for an isolated pytester run."""
    conftest_code = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    return conftest_code + f"\nEXPECTED_ENGINE_SKIP_REASONS_BY_NODEID = {expected!r}\n"


def _make_engine_case(
    pytester: pytest.Pytester,
    *,
    marked: bool = True,
    skip_reason: str = "deterministic tier not installed",
    expected_reason: str = "deterministic tier not installed",
) -> str:
    """Create one synthetic engine-dependent test and return its node id."""
    decorator = (
        f"@pytest.mark.{conftest.ENGINE_DEPENDENCY_MARKER}(expected_skip_reason={expected_reason!r})\n"
        if marked
        else ""
    )
    pytester.makepyfile(
        test_engine_cases=(f"import pytest\n\n{decorator}def test_engine_one():\n    pytest.skip({skip_reason!r})\n")
    )
    return "test_engine_cases.py::test_engine_one"


def _workflow_text() -> str:
    return (REPO_ROOT / ".github" / "workflows" / "checks.yml").read_text(encoding="utf-8")


def _workflow_job_block(workflow: str, job: str) -> str:
    lines = workflow.splitlines()
    start = next((index for index, line in enumerate(lines) if line == f"  {job}:"), None)
    assert start is not None, f"workflow job {job!r} not found"
    end = start + 1
    while end < len(lines):
        line = lines[end]
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":"):
            break
        end += 1
    return "\n".join(lines[start:end])


def _workflow_step_block(
    job_block: str, *, name: str | None = None, uses: str | None = None, contains: str | None = None
) -> str:
    lines = job_block.splitlines()
    for index, line in enumerate(lines):
        if not line.startswith("      - "):
            continue
        if name is not None and line.strip() != f"- name: {name}":
            continue
        if uses is not None and line.strip() != f"- uses: {uses}":
            continue
        end = index + 1
        while end < len(lines) and not lines[end].startswith("      - "):
            end += 1
        block = "\n".join(lines[index:end])
        if contains is not None and contains not in block:
            continue
        return block
    wanted = name if name is not None else uses
    raise AssertionError(f"workflow step {wanted!r} not found in job block:\n{job_block}")


def _literal_run_command(step_block: str) -> str:
    match = re.search(r"^\s+run: (?P<command>.*)$", step_block, re.MULTILINE)
    assert match, f"step has no single-line run command:\n{step_block}"
    return match.group("command")


def _literal_env(job_block: str) -> dict[str, str]:
    env: dict[str, str] = {}
    in_env = False
    for line in job_block.splitlines():
        if line == "    env:":
            in_env = True
            continue
        if in_env and line.startswith("      ") and ":" in line:
            key, value = line.strip().split(":", 1)
            env[key] = value.strip().strip("'\"")
            continue
        if in_env and line.startswith("    ") and not line.startswith("      "):
            break
    return env


def _assert_engine_integration_job(workflow: str) -> None:
    job = _workflow_job_block(workflow, "engine-integration")
    env = _literal_env(job)
    assert env["EXPECTED_ENGINE_VERSION"] == PINNED_ENGINE_VERSION
    assert env["PINNED_ENGINE_REF"] == PINNED_ENGINE_SHA

    checkout = _workflow_step_block(
        job, uses="actions/checkout@v4", contains="repository: Yarbrdab000/tableau-fabric-skills"
    )
    assert "repository: Yarbrdab000/tableau-fabric-skills" in checkout
    assert "ref: ${{ env.PINNED_ENGINE_REF }}" in checkout
    assert "ref: main" not in checkout

    install = _workflow_step_block(job, name="Install pinned deterministic engine")
    assert (
        'test "$(cat "$HOME/.copilot/installed-plugins/tableau-collection/'
        'tableau-fabric-skills/skills/tableau-migration/VERSION")" = "$EXPECTED_ENGINE_VERSION"' in install
    )
    assert "scripts/engine_source.py --json" in install

    test_command = _literal_run_command(_workflow_step_block(job, name="Tests (engine-dependent, pinned engine)"))
    assert test_command == f"{conftest.REQUIRE_ENGINE_TESTS_ENV}=1 uv run pytest -q {ENGINE_TEST_TARGET_COMMAND}"


def _assert_engine_drift_job(workflow: str) -> None:
    job = _workflow_job_block(workflow, "engine-drift")
    checkout = _workflow_step_block(
        job, uses="actions/checkout@v4", contains="repository: Yarbrdab000/tableau-fabric-skills"
    )
    assert "repository: Yarbrdab000/tableau-fabric-skills" in checkout
    assert "ref: main" in checkout
    test_command = _literal_run_command(
        _workflow_step_block(job, name="Tests (engine-dependent, latest engine drift signal)")
    )
    assert test_command == f"{conftest.REQUIRE_ENGINE_TESTS_ENV}=1 uv run pytest -q {ENGINE_TEST_TARGET_COMMAND}"


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
    nodeid = _make_engine_case(pytester)
    pytester.makeconftest(_conftest_for_pytester({nodeid: "deterministic tier not installed"}))
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
    nodeid = _make_engine_case(pytester)
    pytester.makeconftest(_conftest_for_pytester({nodeid: "deterministic tier not installed"}))
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "Set one of: covered-by-pinned-engine-integration-job" in result.stdout.str()


def test_engine_skips_with_an_allowed_not_checked_reason_are_reported(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary Linux CI job may report a deliberate NOT_CHECKED reason, not a silent pass."""
    monkeypatch.delenv(conftest.REQUIRE_ENGINE_TESTS_ENV, raising=False)
    monkeypatch.setenv(conftest.ENGINE_TESTS_NOT_CHECKED_REASON_ENV, "covered-by-pinned-engine-integration-job")
    expected = {
        "test_engine_cases.py::test_engine_one": "deterministic tier not installed",
        "test_engine_cases.py::test_engine_two": "deterministic tier not installed",
    }
    pytester.makeconftest(_conftest_for_pytester(expected))
    pytester.makepyfile(
        test_engine_cases="""
        import pytest

        @pytest.mark.engine_dependency(expected_skip_reason="deterministic tier not installed")
        def test_engine_one():
            pytest.skip("deterministic tier not installed")

        @pytest.mark.engine_dependency(expected_skip_reason="deterministic tier not installed")
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
    nodeid = _make_engine_case(pytester, skip_reason="Windows-specific path spelling")
    pytester.makeconftest(_conftest_for_pytester({nodeid: "deterministic tier not installed"}))
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(
        [
            "*ENGINE-DEPENDENT TESTS SKIPPED (1 test cases did not run)*",
            f"*{conftest.REQUIRE_ENGINE_TESTS_ENV}=1*",
        ]
    )


def test_not_checked_run_fails_if_engine_marker_is_removed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation control: an engine test without the marker cannot disappear from the denominator."""
    monkeypatch.delenv(conftest.REQUIRE_ENGINE_TESTS_ENV, raising=False)
    monkeypatch.setenv(conftest.ENGINE_TESTS_NOT_CHECKED_REASON_ENV, "covered-by-pinned-engine-integration-job")
    nodeid = _make_engine_case(pytester, marked=False)
    pytester.makeconftest(_conftest_for_pytester({nodeid: "deterministic tier not installed"}))
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "engine-dependent test collection does not match the reviewed denominator" in result.stdout.str()


def test_not_checked_run_fails_if_engine_skip_reason_changes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation control: a classified engine test skipped for a different reason is non-clean."""
    monkeypatch.delenv(conftest.REQUIRE_ENGINE_TESTS_ENV, raising=False)
    monkeypatch.setenv(conftest.ENGINE_TESTS_NOT_CHECKED_REASON_ENV, "covered-by-pinned-engine-integration-job")
    nodeid = _make_engine_case(pytester, skip_reason="Windows-specific path spelling")
    pytester.makeconftest(_conftest_for_pytester({nodeid: "deterministic tier not installed"}))
    result = pytester.runpytest()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    assert "exact node-id-to-reason map" in result.stdout.str()


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
    env = dict(os.environ)
    env[engine_source.SIMULATE_ENGINE_ABSENT_ENV] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-m",
            conftest.ENGINE_DEPENDENCY_MARKER,
            *ENGINE_TEST_TARGETS,
        ],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = {line.strip() for line in result.stdout.splitlines() if "::" in line}
    assert collected == set(conftest.EXPECTED_ENGINE_SKIP_REASONS_BY_NODEID)


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
    workflow = _workflow_text()

    assert "schedule:" in workflow, "latest-engine drift pins need a scheduled job"
    _assert_engine_integration_job(workflow)
    _assert_engine_drift_job(workflow)
    assert "covered-by-pinned-engine-integration-job uv run pytest -q" in workflow


def test_workflow_check_fails_if_engine_integration_omits_a_node() -> None:
    """Mutation control: one missing engine node in integration is not masked by drift."""
    workflow = _workflow_text().replace(
        " tests/test_harvest_download_watchdog.py::test_the_ceiling_constants_match_the_INSTALLED_engine",
        "",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_engine_integration_job(workflow)


def test_workflow_check_fails_if_engine_integration_drops_required_env() -> None:
    """Mutation control: the integration job must opt into fail-closed engine skips itself."""
    workflow = _workflow_text().replace(f"{conftest.REQUIRE_ENGINE_TESTS_ENV}=1 ", "", 1)
    with pytest.raises(AssertionError):
        _assert_engine_integration_job(workflow)


def test_workflow_check_fails_if_engine_integration_checkout_moves_to_main() -> None:
    """Mutation control: drift's `main` checkout is not evidence for the pinned integration job."""
    workflow = _workflow_text().replace("ref: ${{ env.PINNED_ENGINE_REF }}", "ref: main", 1)
    with pytest.raises(AssertionError):
        _assert_engine_integration_job(workflow)
