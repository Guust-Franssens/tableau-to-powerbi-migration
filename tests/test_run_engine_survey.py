"""Regression tests for the wrapper around the engine's hand-run estate survey."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import run_engine_survey as survey  # noqa: E402  # pylint: disable=wrong-import-position


class ExecCalled(Exception):
    """Raised by the exec fake so tests can inspect its arguments."""


def test_main_bridges_the_documented_secret_and_adds_no_prompt(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("TABLEAU_PAT_SECRET=documented-secret\n", encoding="utf-8")
    called: dict[str, object] = {}

    def fake_exec(executable, command, env):
        called.update(executable=executable, command=command, env=env)
        raise ExecCalled

    monkeypatch.setattr(survey, "engine_scripts_dir", lambda: tmp_path / "engine-scripts")
    monkeypatch.setattr(survey.os, "execvpe", fake_exec)

    with pytest.raises(ExecCalled):
        survey.main(["--env-file", str(dotenv), "--server", "https://tableau.example", "--json", "survey.json"])

    assert called["executable"] == sys.executable
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

    def fake_exec(_executable, command, env):
        called.update(command=command, env=env)
        raise ExecCalled

    monkeypatch.setattr(survey, "engine_scripts_dir", lambda: tmp_path / "engine-scripts")
    monkeypatch.setattr(survey.os, "execvpe", fake_exec)

    with pytest.raises(ExecCalled):
        survey.main([f"--env-file={dotenv}", "--no-prompt"])

    assert called["command"].count("--no-prompt") == 1
    assert called["env"]["TABLEAU_PAT_SECRET"] == "legacy-secret"
    assert called["env"]["TABLEAU_PAT_VALUE"] == "legacy-secret"


def test_main_accepts_the_engines_bare_env_file_flag(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text("TABLEAU_PAT_SECRET=documented-secret\n", encoding="utf-8")

    def fake_exec(_executable, _command, _env):
        raise ExecCalled

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(survey, "engine_scripts_dir", lambda: tmp_path / "engine-scripts")
    monkeypatch.setattr(survey.os, "execvpe", fake_exec)

    with pytest.raises(ExecCalled):
        survey.main(["--env-file", "--server", "https://tableau.example"])


def test_main_names_the_documented_key_when_the_survey_secret_is_missing(tmp_path):
    with pytest.raises(SystemExit, match="TABLEAU_PAT_SECRET"):
        survey.main(["--env-file", str(tmp_path / "absent.env")])
