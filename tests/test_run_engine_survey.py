"""Regression tests for the wrapper around the engine's hand-run estate survey."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_engine_survey as survey  # noqa: E402  # pylint: disable=wrong-import-position


def _fake_engine(recorded: dict[str, object], returncode: int = 0):
    """A ``subprocess.run`` stand-in that records the engine invocation instead of running it.

    The engine must run as a CHILD process: ``os.exec*`` detaches on Windows and reports success,
    which both destroys the engine's exit code and replaced the pytest process mid-run - so the
    missing-credential test below could not fail (issue #189 review, B1/B3).
    """

    def fake_run(command, env=None, check=False):
        recorded.update(command=command, env=env, check=check)
        return subprocess.CompletedProcess(command, returncode)

    return fake_run


def _no_ambient_secret(monkeypatch) -> None:
    """`resolve_env` layers the process environment over the `.env`, so pin it for these tests."""
    for key in ("TABLEAU_PAT_SECRET", "TABLEAU_PAT_VALUE"):
        monkeypatch.delenv(key, raising=False)


def test_main_bridges_the_documented_secret_and_adds_no_prompt(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("TABLEAU_PAT_SECRET=documented-secret\n", encoding="utf-8")
    called: dict[str, object] = {}

    _no_ambient_secret(monkeypatch)
    monkeypatch.setattr(survey, "engine_scripts_dir", lambda: tmp_path / "engine-scripts")
    monkeypatch.setattr(survey.subprocess, "run", _fake_engine(called))

    with pytest.raises(SystemExit) as excinfo:
        survey.main(["--env-file", str(dotenv), "--server", "https://tableau.example", "--json", "survey.json"])

    assert called, f"the engine was never launched: {excinfo.value}"
    assert called["command"] == [
        sys.executable,
        str(tmp_path / "engine-scripts" / "estate_survey.py"),
        "--env-file",
        str(dotenv),
        "--server",
        "https://tableau.example",
        "--json",
        "survey.json",
        "--no-prompt",
    ]
    assert called["env"]["TABLEAU_PAT_SECRET"] == "documented-secret"
    assert called["env"]["TABLEAU_PAT_VALUE"] == "documented-secret"


def test_main_accepts_the_legacy_engine_secret_without_duplicate_no_prompt(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("TABLEAU_PAT_VALUE=legacy-secret\n", encoding="utf-8")
    called: dict[str, object] = {}

    _no_ambient_secret(monkeypatch)
    monkeypatch.setattr(survey, "engine_scripts_dir", lambda: tmp_path / "engine-scripts")
    monkeypatch.setattr(survey.subprocess, "run", _fake_engine(called))

    with pytest.raises(SystemExit) as excinfo:
        survey.main([f"--env-file={dotenv}", "--no-prompt"])

    assert called, f"the engine was never launched: {excinfo.value}"
    assert called["command"].count("--no-prompt") == 1
    assert called["env"]["TABLEAU_PAT_SECRET"] == "legacy-secret"
    assert called["env"]["TABLEAU_PAT_VALUE"] == "legacy-secret"


def test_main_accepts_the_engines_bare_env_file_flag(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("TABLEAU_PAT_SECRET=documented-secret\n", encoding="utf-8")
    called: dict[str, object] = {}

    _no_ambient_secret(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(survey, "engine_scripts_dir", lambda: tmp_path / "engine-scripts")
    monkeypatch.setattr(survey.subprocess, "run", _fake_engine(called))

    with pytest.raises(SystemExit) as excinfo:
        survey.main(["--env-file", "--server", "https://tableau.example"])

    assert called, f"the bare --env-file flag did not fall back to ./.env: {excinfo.value}"
    assert called["env"]["TABLEAU_PAT_VALUE"] == "documented-secret"


def test_main_propagates_the_engines_exit_code(tmp_path, monkeypatch):
    """`estate_survey.py` returns 1 on unresolved dependencies or a degraded survey - keep it.

    `os.execvpe` detached on Windows and reported 0 unconditionally, so an incomplete survey (#99)
    looked like a clean one and the runbook's next step ran before the JSON existed.
    """
    dotenv = tmp_path / ".env"
    dotenv.write_text("TABLEAU_PAT_SECRET=documented-secret\n", encoding="utf-8")

    _no_ambient_secret(monkeypatch)
    monkeypatch.setattr(survey, "engine_scripts_dir", lambda: tmp_path / "engine-scripts")
    monkeypatch.setattr(survey.subprocess, "run", _fake_engine({}, returncode=1))

    with pytest.raises(SystemExit) as excinfo:
        survey.main(["--env-file", str(dotenv)])

    assert excinfo.value.code == 1


def test_help_needs_no_credential_and_launches_nothing(monkeypatch, capsys):
    """`--help` is the first thing an operator types; it must not demand a PAT (or an engine)."""
    launched: list[object] = []

    _no_ambient_secret(monkeypatch)
    monkeypatch.setattr(survey, "engine_scripts_dir", lambda: pytest.fail("engine resolved for --help"))
    monkeypatch.setattr(survey.subprocess, "run", lambda *a, **k: launched.append(a))

    survey.main(["--help"])

    assert not launched
    assert "usage:" in capsys.readouterr().out


def test_main_names_the_documented_key_when_the_survey_secret_is_missing(tmp_path, monkeypatch):
    """The #183 fix itself: no secret must fail fast, naming the ONE documented spelling.

    Both the engine and the subprocess are faked so that deleting the `require()` gate fails THIS
    assertion rather than dying on an unrelated missing plugin - and so that the credential check is
    proven to happen BEFORE the engine is launched.
    """
    launched: dict[str, object] = {}

    _no_ambient_secret(monkeypatch)
    monkeypatch.setattr(survey, "engine_scripts_dir", lambda: tmp_path / "engine-scripts")
    monkeypatch.setattr(survey.subprocess, "run", _fake_engine(launched))

    with pytest.raises(SystemExit) as excinfo:
        survey.main(["--env-file", str(tmp_path / "absent.env")])

    assert "TABLEAU_PAT_SECRET" in str(excinfo.value)
    assert not launched, "the engine was launched without a credential check"
