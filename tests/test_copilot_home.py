"""Path-only Copilot home selection controls."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from copilot_home import copilot_home  # noqa: E402


@pytest.mark.parametrize("configured", [None, ""])
def test_unset_or_empty_home_uses_exact_default(tmp_path, monkeypatch, configured):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if configured is None:
        monkeypatch.delenv("COPILOT_HOME", raising=False)
    else:
        monkeypatch.setenv("COPILOT_HOME", configured)
    assert copilot_home() == tmp_path / ".copilot"


@pytest.mark.parametrize("exists", [True, False])
def test_configured_home_never_substitutes_default(tmp_path, monkeypatch, exists):
    default = tmp_path / "user" / ".copilot"
    default.mkdir(parents=True)
    configured = tmp_path / "relocated"
    if exists:
        configured.mkdir()
    monkeypatch.setattr(Path, "home", lambda: default.parent)
    monkeypatch.setenv("COPILOT_HOME", str(configured))
    assert copilot_home() == configured


def test_relative_home_is_selected_without_normalisation(monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", "relative-profile")
    assert copilot_home() == Path("relative-profile")


def test_helper_reads_environment_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "first"))
    assert copilot_home() == tmp_path / "first"
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "second"))
    assert copilot_home() == tmp_path / "second"
