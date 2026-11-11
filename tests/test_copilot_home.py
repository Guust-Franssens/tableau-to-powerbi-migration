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


def test_configured_home_is_read_at_call_time_and_never_substituted(tmp_path, monkeypatch):
    """A missing or relative configured home is returned as-is; the default is never substituted."""
    default = tmp_path / "user" / ".copilot"
    default.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: default.parent)
    for configured in (tmp_path / "missing", Path("relative-profile")):
        monkeypatch.setenv("COPILOT_HOME", str(configured))
        assert copilot_home() == configured
