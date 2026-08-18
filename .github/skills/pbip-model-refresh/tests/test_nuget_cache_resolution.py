"""Regression tests for NuGet global-packages cache resolution."""

import sys
from pathlib import Path

SKILL_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SKILL_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SKILL_SCRIPTS))

# The path above must be in place before the skill's modules import, hence the E402 waiver.
# pylint: disable=wrong-import-position,import-error
import probe_desktop_query  # noqa: E402
import refresh_pbip_model  # noqa: E402


def _assert_cache_root(expected_root: Path) -> None:
    assert probe_desktop_query.nuget_packages_root() == expected_root
    assert probe_desktop_query.adomd_dll_globs() == [
        str(
            expected_root
            / "microsoft.analysisservices.adomdclient.netcore*"
            / "**"
            / "Microsoft.AnalysisServices.AdomdClient.dll"
        )
    ]
    assert refresh_pbip_model.amo_dll_glob() == str(
        expected_root / "**" / "netcoreapp*" / "Microsoft.AnalysisServices*.dll"
    )


def test_nuget_packages_env_override_roots_probe_and_refresh_globs(monkeypatch) -> None:
    """A non-empty NUGET_PACKAGES value roots both ADOMD and AMO globs."""
    override = Path.cwd() / "_nuget_packages_for_test"
    monkeypatch.setenv("NUGET_PACKAGES", str(override))

    _assert_cache_root(override)


def test_unset_nuget_packages_uses_home_cache(monkeypatch) -> None:
    """An unset NUGET_PACKAGES value falls back to the default home cache."""
    monkeypatch.delenv("NUGET_PACKAGES", raising=False)

    _assert_cache_root(Path.home() / ".nuget" / "packages")


def test_empty_nuget_packages_falls_back_to_home_cache(monkeypatch) -> None:
    """An empty NUGET_PACKAGES value is treated like unset, not as a root path."""
    monkeypatch.setenv("NUGET_PACKAGES", "")

    _assert_cache_root(Path.home() / ".nuget" / "packages")
