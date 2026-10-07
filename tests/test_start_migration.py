"""Direct offline controls for the approved five-stage migration front door."""

from __future__ import annotations

import json
import sqlite3
import subprocess
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
OTHER_PROJECT = "dddddddd-4444-4444-8444-dddddddddddd"
EXTRA_WORKBOOK = "eeeeeeee-5555-4555-8555-eeeeeeeeeeee"
SESSION = "11111111-2222-4333-8444-555555555555"
ENV_SESSION = "99999999-2222-4333-8444-555555555555"
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


def _write_assessment(out: Path, *, duplicate_project: bool = False, extra_workbook_project: str | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = [{"luid": WORKBOOK}]
    if extra_workbook_project:
        rows.append({"luid": EXTRA_WORKBOOK})
    (out / "assessment.json").write_text(json.dumps({"workbooks": rows}), encoding="utf-8")
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
    if duplicate_project:
        connection.execute("INSERT INTO project VALUES ('dddddddd-4444-4444-8444-dddddddddddd', 'Finance')")
    if extra_workbook_project:
        connection.execute("INSERT OR IGNORE INTO project VALUES (?, 'Other')", (OTHER_PROJECT,))
        connection.execute(
            "INSERT INTO workbook VALUES (?, 'Extra workbook', ?)", (EXTRA_WORKBOOK, extra_workbook_project)
        )
    connection.commit()
    connection.close()


def _write_harvest(out: Path, *, outcome: str = "theirs_only") -> None:
    out.mkdir(parents=True, exist_ok=True)
    row = {"kind": "workbook", "luid": WORKBOOK}
    if outcome == "never_downloaded":
        row["download_error"] = "synthetic download failure"
    else:
        ours, theirs = {
            "ours_only": (True, False),
            "theirs_only": (False, True),
            "both_fail": (False, False),
            "invalid": (None, True),
        }[outcome]
        row.update(ours={"ok": ours}, theirs={"ok": theirs})
    (out / "parse-sweep.json").write_text(
        json.dumps(
            [
                {"kind": "datasource", "luid": DATASOURCE, "ours": {"ok": True}, "theirs": {"ok": True}},
                row,
            ]
        ),
        encoding="utf-8",
    )
    (out / "parse-sweep-totals.json").write_text(
        json.dumps(
            {
                "total": 2,
                "both_ok": 1,
                "ours_only": int(outcome == "ours_only"),
                "theirs_only": int(outcome == "theirs_only"),
                "both_fail": int(outcome == "both_fail"),
                "invalid": int(outcome == "invalid"),
                "never_downloaded": int(outcome == "never_downloaded"),
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
        (out / "input_manifest.json").write_text(json.dumps({"scope_bridge": {"status": bridge}}), encoding="utf-8")


def _script_name(command: list[str]) -> str:
    return next(Path(part).name for part in command if part.endswith(".py"))


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
        malformed_survey: bool = False,
        missing_oracle: bool = False,
        duplicate_project: bool = False,
        extra_workbook_project: str | None = None,
        harvest_outcome: str = "theirs_only",
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
        self.malformed_survey = malformed_survey
        self.missing_oracle = missing_oracle
        self.duplicate_project = duplicate_project
        self.extra_workbook_project = extra_workbook_project
        self.harvest_outcome = harvest_outcome
        self.commands: list[list[str]] = []
        self.options: list[dict] = []
        self.before_exit = []
        self.cancelled = []
        self.waits = []
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
                self.code = pipeline.exit_codes.get(_script_name(command), 0)
                pipeline.commands.append(self.command)
                pipeline.options.append(kwargs)
                print(f"producer-stdout:{_script_name(command)}")
                print(f"producer-stderr:{_script_name(command)}", file=sys.stderr)
                print("OK")
                pipeline._emit_artifacts(self.command)

            def __enter__(self):
                return self

            def __exit__(self, _kind, _value, _traceback):
                return False

            @property
            def pid(self):
                return 43210

            def poll(self):
                if pipeline.interrupt and _script_name(self.command) == "assess_estate.py":
                    raise KeyboardInterrupt
                if self.poll_count == 0:
                    self.poll_count += 1
                    return None
                pipeline.before_exit.append(capsys.readouterr())
                self.returncode = self.code
                return self.returncode

            def wait(self, timeout=None):
                pipeline.waits.append((_script_name(self.command), timeout))
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
        name = _script_name(command)

        def arg(flag: str) -> Path:
            return Path(command[command.index(flag) + 1])

        if name == "run_engine_survey.py":
            survey = self.survey or _survey(project="--project" in command, workbook="--workbook" in command)
            target = arg("--json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("not-json" if self.malformed_survey else json.dumps(survey), encoding="utf-8")
        elif name == "assess_estate.py":
            _write_assessment(
                arg("--out"),
                duplicate_project=self.duplicate_project,
                extra_workbook_project=self.extra_workbook_project,
            )
        elif name == "harvest_estate_assets.py":
            _write_harvest(arg("--out"), outcome=self.harvest_outcome)
        elif name == "capture_tableau_oracle.py":
            if not self.missing_oracle:
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


@pytest.fixture(name="guard_repo")
def guard_repo_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Use real Git ignore decisions, but stop before any producer or service access."""
    root = tmp_path / "guard-repo"
    root.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True, capture_output=True)
    monkeypatch.setattr(frontdoor, "REPO_ROOT", root)
    monkeypatch.setattr(work_dirs, "REPO_ROOT", root)
    monkeypatch.setattr(frontdoor, "resolve_env", lambda _path: dict(ENV))
    monkeypatch.setattr(frontdoor, "_preflight", lambda _env: 7)
    monkeypatch.setattr(frontdoor, "_run_child", lambda *_args: pytest.fail("no producer may run in guard controls"))
    return root


@pytest.mark.parametrize("ignore_manifest", [False, True])
def test_output_guard_checks_run_manifest_before_allocation(guard_repo, capsys, ignore_manifest):
    rules = (
        "_runs/*/assessment/\n"
        "_runs/*/assets/\n"
        "_runs/*/oracle/\n"
        "_runs/*/bundle/\n"
        "_runs/*/parse-sweep.json\n"
        "_runs/*/parse-sweep.md\n"
        "_runs/*/parse-sweep-totals.json\n"
    )
    if ignore_manifest:
        rules += "_runs/*/run.json\n"
    (guard_repo / ".gitignore").write_text(rules, encoding="utf-8")
    probe = subprocess.run(
        ["git", "check-ignore", "-q", "--", "_runs/001-site/run.json"],
        cwd=guard_repo,
        check=False,
        capture_output=True,
    )
    assert probe.returncode == (0 if ignore_manifest else 1)

    code = frontdoor.main([])

    out, err = capsys.readouterr()
    manifests = list(guard_repo.rglob("run.json"))
    assert code == 1
    if ignore_manifest:
        assert manifests == [guard_repo / "_runs" / "001-site" / "run.json"]
        assert "Migration preflight FAILED exit=7" in out
    else:
        assert manifests == [], "the output guard wrote a commit-visible run.json"
        assert "Output guard refused before allocation" in err
        assert "Migration preflight" not in out


def test_front_door_guard_refusal_never_offers_harvest_override(guard_repo, capsys, caplog):
    code = frontdoor.main([])

    out, err = capsys.readouterr()
    assert code == 1
    assert not list(guard_repo.rglob("run.json"))
    assert "Output guard refused before allocation" in err
    assert "--runs-parent" in err
    assert "REFUSING to write customer content" in caplog.text
    assert "Nothing was downloaded." in caplog.text
    assert "--allow-unignored-out" not in out + err + caplog.text


def test_site_run_streams_children_allocates_under_toolkit_and_has_honest_handoff(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("COPILOT_AGENT_SESSION_ID", raising=False)
    unrelated_cwd = tmp_path / "caller-cwd"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
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
    assert f"BUNDLE {run / 'bundle'} (present)" in out
    assert f"REFERENCES {run / 'oracle'} (present)" in out
    assert "fidelity NOT VERIFIED" in out


def test_intersection_scope_uses_resolved_workbook_id_and_session_precedence(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COPILOT_AGENT_SESSION_ID", ENV_SESSION)
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    code = frontdoor.main(["--project", "Finance", "--workbook", WORKBOOK, "--session-id", SESSION])

    out, _err = _captured(capsys, pipeline)
    run = next(work_dirs.runs_root(pipeline.root).glob("001-*"))
    assert code == 0
    harvest_command = next(cmd for cmd in pipeline.commands if _script_name(cmd) == "harvest_estate_assets.py")
    assert "--workbook-id" in harvest_command
    assert harvest_command[harvest_command.index("--workbook-id") + 1] == WORKBOOK
    assert "--project-id" not in harvest_command
    oracle_command = next(cmd for cmd in pipeline.commands if _script_name(cmd) == "capture_tableau_oracle.py")
    assert oracle_command[oracle_command.index("--workbook-id") + 1] == WORKBOOK
    assert "--server" in pipeline.commands[0] and "--pat-name" in pipeline.commands[0]
    assert ENV["TABLEAU_PAT_SECRET"] not in " ".join(pipeline.commands[0])
    manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert manifest["scope"]["selection"] == {"project": "Finance", "workbook": WORKBOOK}
    assert manifest["attribution"] == {"driver": "copilot", "session_id": SESSION}
    assert "NEXT STEPS" in out


def test_environment_session_id_is_used_when_no_explicit_id_is_supplied(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COPILOT_AGENT_SESSION_ID", ENV_SESSION)
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    code = frontdoor.main([])

    run = work_dirs.runs_root(pipeline.root) / "001-site"
    manifest = json.loads((run / "run.json").read_text(encoding="utf-8"))
    assert code == 0
    assert manifest["attribution"] == {"driver": "copilot", "session_id": ENV_SESSION}


def test_project_only_scope_keeps_standalone_project_datasource_selection(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    code = frontdoor.main(["--project", PROJECT])

    _out, _err = _captured(capsys, pipeline)
    harvest_command = next(cmd for cmd in pipeline.commands if _script_name(cmd) == "harvest_estate_assets.py")
    assert code == 0
    assert harvest_command[harvest_command.index("--project-id") + 1] == PROJECT
    assert "--workbook-id" not in harvest_command


@pytest.mark.parametrize("workbook", ["Finance", WORKBOOK])
def test_workbook_only_scope_uses_exact_survey_and_catalog_selection(monkeypatch, tmp_path, capsys, workbook):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    code = frontdoor.main(["--workbook", workbook])

    _captured(capsys, pipeline)
    assert code == 0
    harvest_command = next(cmd for cmd in pipeline.commands if _script_name(cmd) == "harvest_estate_assets.py")
    assert harvest_command[harvest_command.index("--workbook-id") + 1] == WORKBOOK


@pytest.mark.parametrize(
    ("selectors", "extra_project"),
    [
        (["--project", "Finance"], OTHER_PROJECT),
        (["--workbook", WORKBOOK], OTHER_PROJECT),
        (["--workbook", "Finance"], PROJECT),
        (["--project", PROJECT, "--workbook", WORKBOOK], OTHER_PROJECT),
        (["--project", "Finance", "--workbook", WORKBOOK], PROJECT),
    ],
    ids=[
        "project",
        "workbook-other-project",
        "workbook-same-project",
        "combined-other-project",
        "combined-same-project",
    ],
)
def test_extra_survey_workbook_refuses_before_harvest(monkeypatch, tmp_path, capsys, selectors, extra_project):
    survey = _survey(project="--project" in selectors, workbook="--workbook" in selectors)
    survey["workbooks"].append(
        {
            "luid": EXTRA_WORKBOOK,
            "name": "Extra workbook",
            "project": "Finance" if extra_project == PROJECT else "Other",
        }
    )
    survey["scope"]["workbooks_selected"] = 2
    survey["scope"]["workbooks_on_site"] = 2
    survey["summary"]["workbooks_total"] = 2
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, survey=survey, extra_workbook_project=extra_project)

    code = frontdoor.main(selectors)

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert "survey workbook LUIDs do not equal the requested scope" in out
    assert [_script_name(command) for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
    ]


def test_explicit_short_runs_parent_is_used_as_allocator_repo_root(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    parent = tmp_path / "short"

    code = frontdoor.main(["--runs-parent", str(parent)])

    out, _err = _captured(capsys, pipeline)
    run = work_dirs.runs_root(parent) / "001-site"
    assert code == 0
    assert out.splitlines()[0] == str(run)
    assert (run / "run.json").is_file()
    assert not work_dirs.runs_root(pipeline.root).exists()


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
    assert pipeline.guard_calls[0][1] is None
    assert "run.json" in pipeline.guard_calls[0][2]["artifacts"]
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
    assert [_script_name(command) for command in pipeline.commands] == ["run_engine_survey.py"]


def test_empty_project_scope_explains_datasource_only_ambiguity(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, survey=_survey(project=True, empty=True))

    code = frontdoor.main(["--project", "Datasources"])

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert "cannot distinguish a missing project from a datasource-only project" in out
    assert len(pipeline.commands) == 1


def test_ambiguous_project_name_refuses_before_harvest(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, duplicate_project=True)

    code = frontdoor.main(["--project", "Finance"])

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert "project name is ambiguous; use its LUID" in out
    assert [_script_name(command) for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
    ]


def test_changed_workbook_selection_refuses_instead_of_falling_back_to_site(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    code = frontdoor.main(["--workbook", "Missing workbook"])

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert "requested workbook did not match" in out
    assert [_script_name(command) for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
    ]


def test_zero_exit_malformed_survey_is_unknown_and_blocks_dependents(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, malformed_survey=True)

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert "survey is missing or unreadable" in out
    assert len(pipeline.commands) == 1


def test_missing_survey_count_stays_unknown_and_blocks_dependents(monkeypatch, tmp_path, capsys):
    survey = _survey()
    del survey["scope"]["workbooks_on_site"]
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, survey=survey)

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert "site-wide survey completeness is unavailable" in out
    assert [_script_name(command) for command in pipeline.commands] == ["run_engine_survey.py"]


def test_preflight_failure_stops_before_any_producer(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    monkeypatch.setattr(frontdoor, "_preflight", lambda _env: 7)

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 1
    assert pipeline.commands == []
    assert "Migration preflight FAILED exit=7" in out
    run = work_dirs.runs_root(pipeline.root) / "001-site"
    assert (run / "bundle").is_dir() and (run / "oracle").is_dir()
    assert f"BUNDLE {run / 'bundle'} (absent)" in out
    assert f"REFERENCES {run / 'oracle'} (absent)" in out


def test_failed_harvest_does_not_launch_reference_or_bundle(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, exit_codes={"harvest_estate_assets.py": 3})

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 1
    assert [_script_name(command) for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
        "harvest_estate_assets.py",
    ]
    assert "harvest - FAILED exit=3" in out


@pytest.mark.parametrize("outcome", ["never_downloaded", "invalid"])
def test_zero_exit_incomplete_harvest_blocks_both_dependents(monkeypatch, tmp_path, capsys, outcome):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, harvest_outcome=outcome)

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert "harvest - CANNOT_ESTABLISH exit=0" in out
    assert f"{outcome}=1" in out
    assert [_script_name(command) for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
        "harvest_estate_assets.py",
    ]


@pytest.mark.parametrize("outcome", ["ours_only", "theirs_only", "both_fail"])
def test_assessed_parser_failures_do_not_become_harvest_refusals(monkeypatch, tmp_path, capsys, outcome):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, harvest_outcome=outcome)

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 0
    assert "harvest - OK exit=0" in out
    assert [_script_name(command) for command in pipeline.commands][-2:] == [
        "capture_tableau_oracle.py",
        "run_estate.py",
    ]


def test_zero_exit_reference_with_missing_manifest_still_attempts_bundle_but_cannot_pass(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, missing_oracle=True)

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 3
    assert [_script_name(command) for command in pipeline.commands][-2:] == [
        "capture_tableau_oracle.py",
        "run_estate.py",
    ]
    assert "oracle manifest is missing or unreadable" in out
    run = work_dirs.runs_root(pipeline.root) / "001-site"
    assert f"BUNDLE {run / 'bundle'} (present)" in out
    assert f"REFERENCES {run / 'oracle'} (absent)" in out


def test_native_child_failure_wins_over_console_ok_text(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, exit_codes={"run_engine_survey.py": 7})

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 1
    assert "OK" in out
    assert "survey - FAILED exit=7" in out
    assert len(pipeline.commands) == 1


@pytest.mark.parametrize("oracle_code", [1, 2, 3, 4, 5])
def test_reference_failure_is_reported_but_bundle_still_runs(monkeypatch, tmp_path, capsys, oracle_code):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, exit_codes={"capture_tableau_oracle.py": oracle_code})

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    names = [_script_name(command) for command in pipeline.commands]
    assert code == 1
    assert names[-2:] == ["capture_tableau_oracle.py", "run_estate.py"]
    assert f"reference - FAILED exit={oracle_code}" in out


@pytest.mark.parametrize("bridge", [None, "unestablished"])
def test_zero_exit_bundle_without_established_scope_bridge_is_not_success(monkeypatch, tmp_path, capsys, bridge):
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


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_interrupt_cancels_current_child_and_launches_no_later_stage(monkeypatch, tmp_path, capsys, platform):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, interrupt=True)
    monkeypatch.setattr(
        frontdoor, "sys", SimpleNamespace(platform=platform, executable=sys.executable, stderr=sys.stderr)
    )
    monkeypatch.setattr(
        frontdoor.os, "killpg", lambda _pid, _signal: pipeline.cancelled.append("killpg"), raising=False
    )

    code = frontdoor.main([])

    out, _err = _captured(capsys, pipeline)
    assert code == 130
    assert pipeline.cancelled == (["signal"] if platform == "win32" else ["killpg"])
    assert pipeline.waits[-1] == ("assess_estate.py", 2)
    assert [_script_name(command) for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
    ]
    assert "Migration interrupted" in out
    assert out.splitlines()[-1].startswith("RUN ")


def test_readme_routes_live_sites_to_front_door_without_preallocation():
    setup = (REPO_ROOT / "scripts" / "README.md").read_text(encoding="utf-8").split("### Run setup\n", 1)[1]
    setup = setup.split("## Migration pipeline", 1)[0]
    assert "For new local folders, workbooks or datasources" in setup
    assert "python -B scripts\\start_migration.py" in setup
    assert "do not pre-allocate a run for live-site invocations" in setup
    assert "For each new site/folder/workbook/datasource" not in setup


def test_invalid_session_id_is_usage_error_before_output_guard(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    with pytest.raises(SystemExit) as excinfo:
        frontdoor.main(["--session-id", "not-a-uuid"])

    assert excinfo.value.code == 2
    assert pipeline.guard_calls == []
    assert pipeline.preflight == [] and pipeline.commands == []


def test_empty_explicit_session_id_does_not_fall_back_to_environment(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("COPILOT_AGENT_SESSION_ID", SESSION)
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    with pytest.raises(SystemExit) as excinfo:
        frontdoor.main(["--session-id", ""])

    assert excinfo.value.code == 2
    assert pipeline.guard_calls == []
    assert pipeline.preflight == [] and pipeline.commands == []
