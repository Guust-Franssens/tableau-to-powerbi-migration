"""Direct offline controls for the approved five-stage migration front door."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import start_migration as frontdoor  # noqa: E402  # pylint: disable=wrong-import-position
import work_dirs  # noqa: E402  # pylint: disable=wrong-import-position

PROJECT = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
WORKBOOK = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
DATASOURCE = "cccccccc-3333-4333-8333-cccccccccccc"
SESSION = "11111111-2222-4333-8444-555555555555"
ENV = {
    "TABLEAU_SERVER_URL": "https://tableau.example.invalid",
    "TABLEAU_SITE": "site",
    "TABLEAU_PAT_NAME": "synthetic-name",
    "TABLEAU_PAT_SECRET": "synthetic-secret",
}


def _survey(*, project: bool = False, workbook: bool = False, unmatched=None, empty: bool = False) -> dict:
    rows = [] if empty else [{"luid": WORKBOOK, "name": "Finance", "project": "Finance"}]
    return {
        "workbooks": rows,
        "required_datasources": [{"luid": DATASOURCE, "datasource_name": "Ledger"}],
        "scope": {
            "scoped": project or workbook,
            "projects": ["Finance"] if project else [],
            "workbooks": ["Finance"] if workbook else [],
            "workbooks_selected": len(rows),
            "workbooks_on_site": len(rows),
            "unmatched": [] if unmatched is None else unmatched,
            "datasource_index": "site-wide",
        },
        "summary": {"scoped": project or workbook, "workbooks_total": len(rows)},
    }


def _write_assessment(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "assessment.json").write_text(json.dumps({"workbooks": [{"luid": WORKBOOK}]}), encoding="utf-8")
    connection = sqlite3.connect(out / "estate.db")
    connection.executescript(
        f"""
        CREATE TABLE project (luid TEXT PRIMARY KEY, name TEXT);
        CREATE TABLE workbook (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE datasource (luid TEXT PRIMARY KEY, name TEXT, project_luid TEXT);
        CREATE TABLE dependency (workbook_luid TEXT, datasource_luid TEXT, datasource_name TEXT);
        INSERT INTO project VALUES ('{PROJECT}', 'Finance');
        INSERT INTO workbook VALUES ('{WORKBOOK}', 'Finance', '{PROJECT}');
        INSERT INTO datasource VALUES ('{DATASOURCE}', 'Ledger', 'project-other');
        INSERT INTO dependency VALUES ('{WORKBOOK}', '{DATASOURCE}', 'Ledger');
        """
    )
    connection.commit()
    connection.close()


def _write_harvest(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "parse-sweep.json").write_text(
        json.dumps(
            [
                {"kind": "datasource", "luid": DATASOURCE, "ours": {"ok": True}, "theirs": {"ok": True}},
                {"kind": "workbook", "luid": WORKBOOK, "ours": {"ok": False}, "theirs": {"ok": True}},
            ]
        ),
        encoding="utf-8",
    )
    (out / "parse-sweep-totals.json").write_text(
        json.dumps(
            {
                "total": 2,
                "both_ok": 1,
                "ours_only": 0,
                "theirs_only": 1,
                "both_fail": 0,
                "invalid": 0,
                "never_downloaded": 0,
            }
        ),
        encoding="utf-8",
    )


def _write_oracle(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "oracle-manifest.json").write_text(
        json.dumps(
            {
                "schema": "tableau-oracle/1",
                "view_count": 1,
                "captured_complete": 1,
                "views": [{"view_luid": "view-id", "workbook_luid": WORKBOOK}],
            }
        ),
        encoding="utf-8",
    )


def _write_bundle(out: Path, *, bridge="established") -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps({"workbooks": [{}], "datasources": [{}]}), encoding="utf-8")
    if bridge is not None:
        (out / "input_manifest.json").write_text(
            json.dumps({"scope_bridge": {"status": bridge}}), encoding="utf-8"
        )


class _Pipeline:
    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture,
        *,
        exit_codes: dict[str, int] | None = None,
        survey: dict | None = None,
        bridge: str | None = "established",
        engine_started: bool = False,
        interrupt: bool = False,
    ) -> None:
        self.root = tmp_path / "toolkit"
        self.root.mkdir()
        monkeypatch.setattr(frontdoor, "REPO_ROOT", self.root)
        monkeypatch.setattr(work_dirs, "REPO_ROOT", self.root)
        monkeypatch.setattr(frontdoor, "resolve_env", lambda _path: dict(ENV))
        self.guard_calls = []
        monkeypatch.setattr(
            frontdoor.harvest,
            "refuse_unignored_output",
            lambda path, allow, **kwargs: self.guard_calls.append((path, allow, kwargs)) or False,
        )
        self.exit_codes = exit_codes or {}
        self.survey = survey
        self.bridge = bridge
        self.engine_started = engine_started
        self.interrupt = interrupt
        self.commands: list[list[str]] = []
        self.options: list[dict] = []
        self.before_exit = []
        self.cancelled = []
        self.preflight = []
        monkeypatch.setattr(
            frontdoor.subprocess,
            "run",
            lambda command, **kwargs: self._preflight(command, kwargs),
        )
        self._patch_clock(monkeypatch)
        pipeline = self

        class FakeProcess:
            def __init__(self, command, **kwargs):
                self.command = list(command)
                self.returncode = None
                self.poll_count = 0
                self.code = pipeline.exit_codes.get(Path(command[3]).name, 0)
                pipeline.commands.append(self.command)
                pipeline.options.append(kwargs)
                print(f"producer-stdout:{Path(command[3]).name}")
                print(f"producer-stderr:{Path(command[3]).name}", file=sys.stderr)
                pipeline._emit_artifacts(self.command)

            @property
            def pid(self):
                return 43210

            def poll(self):
                if pipeline.interrupt and Path(self.command[3]).name == "assess_estate.py":
                    raise KeyboardInterrupt
                if self.poll_count == 0:
                    self.poll_count += 1
                    return None
                pipeline.before_exit.append(capsys.readouterr())
                self.returncode = self.code
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def send_signal(self, _signal):
                pipeline.cancelled.append("signal")

            def terminate(self):
                pipeline.cancelled.append("terminate")

            def kill(self):
                pipeline.cancelled.append("kill")

        monkeypatch.setattr(frontdoor.subprocess, "Popen", FakeProcess)

    def _preflight(self, command, kwargs):
        self.preflight.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    @staticmethod
    def _patch_clock(monkeypatch):
        now = [0.0]

        def tick():
            now[0] += 16.0
            return now[0]

        monkeypatch.setattr(frontdoor.time, "monotonic", tick)
        monkeypatch.setattr(frontdoor.time, "sleep", lambda _seconds: None)

    def _emit_artifacts(self, command: list[str]) -> None:
        name = Path(command[3]).name

        def arg(flag: str) -> Path:
            return Path(command[command.index(flag) + 1])

        if name == "run_engine_survey.py":
            survey = self.survey or _survey(project="--project" in command, workbook="--workbook" in command)
            target = arg("--json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(survey), encoding="utf-8")
        elif name == "assess_estate.py":
            _write_assessment(arg("--out"))
        elif name == "harvest_estate_assets.py":
            _write_harvest(arg("--out"))
        elif name == "capture_tableau_oracle.py":
            _write_oracle(arg("--out"))
        elif name == "run_estate.py":
            if self.engine_started:
                arg("--output").mkdir(parents=True, exist_ok=True)
                (arg("--output") / "engine-output-receipt.json").write_text("{}", encoding="utf-8")
            if self.exit_codes.get("run_estate.py", 0) == 0:
                _write_bundle(arg("--output"), bridge=self.bridge)


def _captured(capsys, pipeline: _Pipeline) -> tuple[str, str]:
    tail = capsys.readouterr()
    out = "".join(piece.out for piece in pipeline.before_exit) + tail.out
    err = "".join(piece.err for piece in pipeline.before_exit) + tail.err
    return out, err


def test_site_run_streams_children_allocates_under_toolkit_and_has_honest_handoff(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("COPILOT_AGENT_SESSION_ID", raising=False)
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    code = frontdoor.main([])

    out, err = _captured(capsys, pipeline)
    run = work_dirs.runs_root(pipeline.root) / "001-site"
    assert code == 0
    assert out.splitlines()[0] == str(run)
    assert out.splitlines()[-1] == f"RUN {run}"
    assert "producer-stdout:run_engine_survey.py" in out
    assert "producer-stderr:run_engine_survey.py" in err
    assert "[1/5] survey - running elapsed=16s" in out
    assert "producer-stdout:run_engine_survey.py" in pipeline.before_exit[0].out
    assert "producer-stderr:run_engine_survey.py" in pipeline.before_exit[0].err
    assert "[1/5] survey - running elapsed=16s" in pipeline.before_exit[0].out
    assert all(entry["stdout"] is None and entry["stderr"] is None for entry in pipeline.options)
    assert all(command[1:3] == ["-u", "-B"] for command in pipeline.commands)
    assert all(entry["env"]["PYTHONUNBUFFERED"] == "1" for entry in pipeline.options)
    assert pipeline.preflight[0][0] == [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(pipeline.root / "scripts" / "preflight.ps1"),
    ]
    assert "-Update" not in pipeline.preflight[0][0]
    manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert manifest["scope"] == {
        "kind": "site",
        "display": "site",
        "server": ENV["TABLEAU_SERVER_URL"],
        "site": "site",
        "selection": {"project": None, "workbook": None},
    }
    assert manifest["assessment_scope"] == "site"
    assert manifest["attribution"] == {"driver": "operator"}
    assert "stages" not in manifest and "preparation" not in manifest
    assert "BUNDLE + REFERENCES CAPTURED" in out
    assert "fidelity NOT VERIFIED" in out


def test_intersection_scope_uses_resolved_workbook_id_and_session_precedence(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("COPILOT_AGENT_SESSION_ID", SESSION)
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    code = frontdoor.main(["--project", "Finance", "--workbook", WORKBOOK, "--session-id", SESSION])

    out, _err = _captured(capsys, pipeline)
    run = next(work_dirs.runs_root(pipeline.root).glob("001-*"))
    assert code == 0
    harvest_command = next(cmd for cmd in pipeline.commands if Path(cmd[3]).name == "harvest_estate_assets.py")
    assert harvest_command[harvest_command.index("--workbook-id") + 1] == WORKBOOK
    assert "--project-id" not in harvest_command
    oracle_command = next(cmd for cmd in pipeline.commands if Path(cmd[3]).name == "capture_tableau_oracle.py")
    assert oracle_command[oracle_command.index("--workbook-id") + 1] == WORKBOOK
    assert "--server" in pipeline.commands[0] and "--pat-name" in pipeline.commands[0]
    assert ENV["TABLEAU_PAT_SECRET"] not in " ".join(pipeline.commands[0])
    manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert manifest["scope"]["selection"] == {"project": "Finance", "workbook": WORKBOOK}
    assert manifest["attribution"] == {"driver": "copilot", "session_id": SESSION}
    assert "NEXT STEPS" in out


def test_output_guard_runs_before_allocation_and_names_runs_parent(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    pipeline.guard_calls.clear()

    def refuse(path, allow, **kwargs):
        pipeline.guard_calls.append((path, allow, kwargs))
        return True

    monkeypatch.setattr(frontdoor.harvest, "refuse_unignored_output", refuse)

    code = frontdoor.main(["--project", "Finance"])

    out, err = capsys.readouterr()
    assert code == 1
    assert not list(work_dirs.runs_root(pipeline.root).glob("*"))
    assert pipeline.preflight == [] and pipeline.commands == []
    assert len(pipeline.guard_calls) == 1
    assert pipeline.guard_calls[0][1] is False
    assert any(path.startswith("assessment/") for path in pipeline.guard_calls[0][2]["artifacts"])
    assert any(path.startswith("assets/") for path in pipeline.guard_calls[0][2]["artifacts"])
    assert any(path.startswith("oracle/") for path in pipeline.guard_calls[0][2]["artifacts"])
    assert any(path.startswith("bundle/") for path in pipeline.guard_calls[0][2]["artifacts"])
    assert "--runs-parent <short path>" in err
    assert not out.strip()


@pytest.mark.parametrize(
    ("survey", "message"),
    [
        (_survey(unmatched=["unknown"]), "unmatched"),
        (_survey(empty=True), "empty"),
    ],
)
def test_zero_exit_invalid_survey_scope_refuses_assessment(monkeypatch, tmp_path, capsys, survey, message):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, survey=survey)

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert message in out
    assert [Path(command[3]).name for command in pipeline.commands] == ["run_engine_survey.py"]


@pytest.mark.parametrize("oracle_code", [1, 2, 3, 4, 5])
def test_reference_failure_is_reported_but_bundle_still_runs(monkeypatch, tmp_path, capsys, oracle_code):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, exit_codes={"capture_tableau_oracle.py": oracle_code})

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    names = [Path(command[3]).name for command in pipeline.commands]
    assert code == 1
    assert names[-2:] == ["capture_tableau_oracle.py", "run_estate.py"]
    assert f"reference - FAILED exit={oracle_code}" in out


@pytest.mark.parametrize("bridge", [None, "unestablished"])
def test_zero_exit_bundle_without_established_scope_bridge_is_not_success(
    monkeypatch, tmp_path, capsys, bridge
):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, bridge=bridge)

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert "scope_bridge" in out
    assert "Overall exit=3" in out


@pytest.mark.parametrize("engine_started", [False, True])
def test_path_ceiling_uses_engine_artifacts_and_suggests_a_new_short_root(
    monkeypatch, tmp_path, capsys, engine_started
):
    pipeline = _Pipeline(
        monkeypatch,
        tmp_path,
        capsys,
        exit_codes={"run_estate.py": 10},
        engine_started=engine_started,
    )

    code = frontdoor.main(["--project", "Finance", "--storage-decision", "policy.json", "--session-id", SESSION])

    out, _err = _captured(capsys, pipeline)
    expected = "bundle was built and retained" if engine_started else "stopped before conversion"
    assert code == 1
    assert expected in out
    assert "new run (old run retained)" in out
    assert "--runs-parent C:/t2p" in out or "--runs-parent C:\\t2p" in out
    assert "--project Finance" in out
    assert "--storage-decision policy.json" in out
    assert "--session-id" in out


def test_interrupt_cancels_current_child_and_launches_no_later_stage(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, interrupt=True)
    monkeypatch.setattr(frontdoor.os, "killpg", lambda _pid, _signal: pipeline.cancelled.append("killpg"))

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 130
    assert pipeline.cancelled == ["killpg"]
    assert [Path(command[3]).name for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
    ]
    assert "Migration interrupted" in out
    assert out.splitlines()[-1].startswith("RUN ")


def test_invalid_session_id_is_usage_error_before_output_guard(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    with pytest.raises(SystemExit) as excinfo:
        frontdoor.main(["--session-id", "not-a-uuid"])

    assert excinfo.value.code == 2
    assert pipeline.guard_calls == []
    assert pipeline.preflight == [] and pipeline.commands == []
