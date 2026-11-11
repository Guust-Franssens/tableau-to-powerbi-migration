"""Direct offline controls for the approved five-stage migration front door."""

from __future__ import annotations

from contextlib import nullcontext
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import assess_estate  # noqa: E402  # pylint: disable=wrong-import-position
import assessment_report  # noqa: E402  # pylint: disable=wrong-import-position
import run_status  # noqa: E402  # pylint: disable=wrong-import-position
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


@pytest.mark.parametrize(
    "state,verdict,native,observed,json_mode",
    [
        ("ok", "PASS", 7, True, False),
        ("over_ceiling", "OVER_CEILING", 10, True, True),
        ("cannot_establish", "CANNOT_ESTABLISH", 10, True, False),
        ("ok", "PASS", 7, False, True),
    ],
)
def test_bundle_child_private_path_gate_channel(tmp_path, capfd, state, verdict, native, observed, json_mode):
    """A real child reports its actual gate over IPC without changing stdout/stderr or exit."""
    import t2p

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for source in (REPO_ROOT / "scripts").glob("*.py"):
        shutil.copyfile(source, scripts / source.name)
    (scripts / "run_estate.py").write_text(
        "import os,sys\n"
        f"STATE={state!r}\n"
        "def project_estate_path_ceiling(*args):\n"
        "    return {'status':STATE,'paths':[] if STATE=='cannot_establish' else [\n"
        "        {'kind':'file','length':210,'ceiling':259,'path':'PRIVATE_PATH_LABEL'},\n"
        "        {'kind':'directory','length':200,'ceiling':247,'path':'PRIVATE_PATH_LABEL'}]}\n"
        "def preflight_estate_path_ceiling(*args):\n"
        "    result=project_estate_path_ceiling(*args)\n"
        "    return result['status']=='ok', 'PRIVATE_GATE_PROSE'\n"
        "def main(argv):\n"
        "    os.write(1,b'NATIVE_CHILD_STDOUT\\n'); os.write(2,b'NATIVE_CHILD_STDERR\\n')\n"
        "    allowed,_=preflight_estate_path_ceiling()\n"
        "    return 7 if allowed else 10\n"
        "if __name__=='__main__': raise SystemExit(main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    records = []
    command = [sys.executable, "-u", "-B", str(scripts / "run_estate.py"), "--input", "synthetic"]
    with t2p._native_to_stderr() if json_mode else nullcontext():
        result = frontdoor._run_child(
            "bundle",
            5,
            command,
            {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            tmp_path / "report.json",
            observer=records.append if observed else None,
        )
    captured = capfd.readouterr()
    assert result.code == native, "structured observer must preserve native child exit"
    assert "NATIVE_CHILD_STDERR" in captured.err
    if json_mode:
        assert not captured.out, "structured observer must preserve JSON stdout isolation"
        assert "NATIVE_CHILD_STDOUT" in captured.err
    else:
        assert "NATIVE_CHILD_STDOUT" in captured.out
    assert "PRIVATE_PATH_LABEL" not in str(records)
    if observed:
        assert len(records) == 1
        assert records[0]["verdict"] == verdict, "actual child owner verdict must reach observer"
        assert records[0]["measurements"] == (
            {}
            if state == "cannot_establish"
            else {
                "file": {"length": 210, "ceiling": 259},
                "directory": {"length": 200, "ceiling": 247},
            }
        )
    else:
        assert records == [], "direct default must not introduce an observer channel"


def _survey(
    *, project: bool = False, workbook: bool = False, unmatched=None, empty: bool = False, missing_luid: bool = False
) -> dict:
    rows = [] if empty else [{"luid": WORKBOOK, "name": "Finance", "project": "Finance"}]
    if missing_luid and rows:
        rows[0].pop("luid")
    return {
        "degraded": False,
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
        "summary": {"scoped": project or workbook, "workbooks_total": len(rows), "degraded": False},
    }


def _write_assessment(
    out: Path,
    *,
    duplicate_project: bool = False,
    extra_workbook_project: str | None = None,
    primary_degradation: bool = False,
    empty_project_luid: bool = False,
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "luid": WORKBOOK,
            "name": "Finance",
            "rank": 1,
            "cumulative_share": 1.0,
            "tier": "migrate",
            "tier_reason": "high usage",
            "complexity": 1.0 if not primary_degradation else 0.0,
            "views_lifetime": 1,
        }
    ]
    if extra_workbook_project:
        rows.append(
            {
                **rows[0],
                "luid": EXTRA_WORKBOOK,
                "name": "Extra workbook",
                "rank": 2,
                "cumulative_share": 1.0,
            }
        )
    listing_errors = [{"listing": "datasources", "severity": "primary", "status": 403}] if primary_degradation else []
    total = len(rows)
    assessment = {
        "degraded": primary_degradation,
        "degraded_primary": primary_degradation,
        "listing_errors": listing_errors,
        "iam_hard_cases": [],
        "datasource_hazards": {},
        "workbooks": rows,
        "summary": {
            "workbooks_total": total,
            "degraded": primary_degradation,
            "degraded_primary": primary_degradation,
        },
    }
    (out / "assessment.json").write_text(json.dumps(assessment), encoding="utf-8")
    connection = sqlite3.connect(out / "estate.db")
    connection.executescript(assess_estate.SCHEMA)
    connection.execute(
        "INSERT INTO project (luid, name, parent_luid, content_permissions) VALUES (?, ?, '', '')",
        (PROJECT, "Finance"),
    )
    connection.execute(
        "INSERT INTO datasource (luid, name, project, project_luid) VALUES (?, ?, ?, ?)",
        (DATASOURCE, "Ledger", "Other", "project-other"),
    )
    for row in rows:
        luid = row["luid"]
        connection.execute(
            "INSERT INTO workbook (luid, name, project, project_luid, sheets, dashboards, calcs, lods, "
            "table_calcs, complexity, complexity_understated, views_lifetime, view_count, rank, "
            "cumulative_share, tier, tier_reason) VALUES (?, ?, ?, ?, 1, 0, 0, 0, 0, ?, 0, 1, 1, ?, ?, ?, ?)",
            (
                luid,
                row["name"],
                "Finance",
                "" if empty_project_luid else PROJECT if luid == WORKBOOK else extra_workbook_project,
                row["complexity"],
                row["rank"],
                row["cumulative_share"],
                row["tier"],
                row["tier_reason"],
            ),
        )
        connection.execute(
            "INSERT INTO view (luid, workbook_luid, name, content_url, views_lifetime) VALUES (?, ?, ?, ?, 1)",
            (f"view-{luid}", luid, "Overview", "overview"),
        )
        connection.execute(
            "INSERT INTO dependency (workbook_name, workbook_luid, datasource_name, datasource_luid, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (row["name"], luid, "Ledger", DATASOURCE, "survey"),
        )
    connection.execute(
        "INSERT INTO assessment_run VALUES ('2026-01-01T00:00:00Z', ?, ?, ?, ?)",
        (int(primary_degradation), int(primary_degradation), total, len(listing_errors)),
    )
    connection.execute("INSERT INTO permission_collection VALUES (0)")
    for error in listing_errors:
        connection.execute(
            "INSERT INTO listing_error (listing, severity, status) VALUES (?, ?, ?)",
            (error["listing"], error["severity"], error["status"]),
        )
    if duplicate_project:
        connection.execute(
            "INSERT INTO project (luid, name, parent_luid, content_permissions) "
            "VALUES ('dddddddd-4444-4444-8444-dddddddddddd', 'Finance', '', '')"
        )
    if extra_workbook_project:
        connection.execute(
            "INSERT OR IGNORE INTO project (luid, name, parent_luid, content_permissions) VALUES (?, 'Other', '', '')",
            (extra_workbook_project,),
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
        primary_degradation: bool = False,
        missing_survey_luid: bool = False,
        collector_outcome: str = "unavailable",
        empty_project_luid: bool = False,
        env_values: dict[str, str] | None = None,
        inconsistent_assessment: bool = False,
    ) -> None:
        self.root = tmp_path / "toolkit"
        self.root.mkdir()
        monkeypatch.setattr(frontdoor, "REPO_ROOT", self.root)
        monkeypatch.setattr(work_dirs, "REPO_ROOT", self.root)
        monkeypatch.setattr(frontdoor, "resolve_env", lambda _path: dict(env_values or ENV))
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
        self.primary_degradation = primary_degradation
        self.missing_survey_luid = missing_survey_luid
        self.collector_outcome = collector_outcome
        self.empty_project_luid = empty_project_luid
        self.inconsistent_assessment = inconsistent_assessment
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
                if _script_name(self.command) == "assessment_report.py":
                    self.code = assessment_report.main(["--run", self.command[self.command.index("--run") + 1]])

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
            survey = self.survey or _survey(
                project="--project" in command,
                workbook="--workbook" in command,
                missing_luid=self.missing_survey_luid,
            )
            target = arg("--json")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("not-json" if self.malformed_survey else json.dumps(survey), encoding="utf-8")
        elif name == "assess_estate.py":
            output = arg("--out")
            _write_assessment(
                output,
                duplicate_project=self.duplicate_project,
                extra_workbook_project=self.extra_workbook_project,
                primary_degradation=self.primary_degradation,
                empty_project_luid=self.empty_project_luid,
            )
            if self.inconsistent_assessment:
                document = json.loads((output / "assessment.json").read_text(encoding="utf-8"))
                document["summary"]["workbooks_total"] = 0
                (output / "assessment.json").write_text(json.dumps(document), encoding="utf-8")
        elif name == "admin_insights_collector.py" and self.exit_codes.get(name, 0) in {0, 3}:
            outcome = self.collector_outcome
            reason = {"unavailable": "datasource_not_found", "cannot_establish": "transport_failure"}.get(outcome)
            result = {
                "schema": "admin-insights-aggregate/1",
                "outcome": outcome,
                "reason": None if outcome == "available" else reason,
                "datasource_luid": DATASOURCE if outcome == "available" else None,
                "window": {"first_day": None, "last_day": None},
                "rows": [],
                "unmapped_events": 0,
            }
            arg("--out").write_text(json.dumps(result), encoding="utf-8")
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


def _assessment_run(pipeline: _Pipeline) -> Path:
    # NTFS lists the hidden ".run-number-reservations" allocator directory first; skip it.
    return next(path for path in (pipeline.root / "_runs").iterdir() if not path.name.startswith("."))


def test_assess_only_runs_four_children_and_publishes_current_html(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, collector_outcome="available")

    assert frontdoor.main(["--assess-only"]) == 0

    run = _assessment_run(pipeline)
    assert [Path(command[3]).name for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
        "admin_insights_collector.py",
        "assessment_report.py",
    ]
    assert not any(
        "harvest_estate_assets.py" in command or "capture_tableau_oracle.py" in command for command in pipeline.commands
    )
    assert json.loads((run / "run.json").read_text(encoding="utf-8"))["intent"] == "assessment"
    assert (run / "assessment" / "assessment-report.html").is_file()
    status, code = run_status.build_status(run)
    assert code == 0
    assert status["assessment"]["assessment"] == "established"
    assert status["assessment"]["admin_insights"] == "available"
    out, _err = _captured(capsys, pipeline)
    assert "ASSESSMENT COMPLETE (not migration readiness)" in out
    assert "t2p start --project <name>" in out
    assert "NEXT STEPS" in out
    html_report = (run / "assessment" / "assessment-report.html").read_text(encoding="utf-8")
    assert 'data-section="3" data-state="ok"' in html_report


def test_assess_only_guard_is_extended_without_changing_start_guard(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    assert frontdoor.main(["--assess-only"]) == 0
    assessment_artifacts = pipeline.guard_calls[0][2]["artifacts"]
    assert assessment_artifacts[-2:] == (
        "assessment/admin_insights.json",
        "assessment/assessment-report.html",
    )

    start_root = tmp_path / "start"
    start_root.mkdir()
    start = _Pipeline(monkeypatch, start_root, capsys)
    assert frontdoor.main([]) == 0
    start_artifacts = start.guard_calls[0][2]["artifacts"]
    assert "assessment/admin_insights.json" not in start_artifacts
    assert "assessment/assessment-report.html" not in start_artifacts


def test_assess_only_allocation_refusal_precedes_any_tableau_child(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)

    def refuse_allocation(*_args, **_kwargs):
        raise PermissionError("synthetic allocation failure")

    monkeypatch.setattr(work_dirs, "allocate_run", refuse_allocation)

    assert frontdoor.main(["--assess-only"]) == 1

    assert pipeline.commands == []
    assert pipeline.preflight == []
    _out, err = _captured(capsys, pipeline)
    assert "Run allocation refused: PermissionError: synthetic allocation failure" in err


@pytest.mark.parametrize(
    ("outcome", "exit_code", "section_state", "reason"),
    [
        ("unavailable", 0, "unavailable", "datasource_not_found"),
        ("cannot_establish", 3, "cannot_establish", "transport_failure"),
    ],
)
def test_assess_only_collector_outcomes_share_report_and_status(
    monkeypatch, tmp_path, capsys, outcome, exit_code, section_state, reason
):
    pipeline = _Pipeline(
        monkeypatch, tmp_path, capsys, collector_outcome=outcome, exit_codes={"admin_insights_collector.py": exit_code}
    )

    assert frontdoor.main(["--assess-only"]) == exit_code

    run = _assessment_run(pipeline)
    status, status_code = run_status.build_status(run)
    assert status_code == (0 if outcome == "unavailable" else 1)
    assert status["assessment"]["admin_insights"] == outcome
    report = (run / "assessment" / "assessment-report.html").read_text(encoding="utf-8")
    assert f'data-section="3" data-state="{section_state}" data-reason="{reason}"' in report
    out, _err = _captured(capsys, pipeline)
    assert ("ASSESSMENT COMPLETE (not migration readiness)" in out) == (exit_code == 0)
    if exit_code == 3:
        assert "ASSESSMENT INCOMPLETE (transport_failure)" in out


def test_assess_only_collector_crash_renders_not_collected_and_status_missing(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, exit_codes={"admin_insights_collector.py": 1})

    assert frontdoor.main(["--assess-only"]) == 1

    run = _assessment_run(pipeline)
    report = (run / "assessment" / "assessment-report.html").read_text(encoding="utf-8")
    assert 'data-section="3" data-state="not_collected" data-reason="collector_no_result"' in report
    status, code = run_status.build_status(run)
    assert code == 1
    assert status["assessment"]["admin_insights"] == "not_collected"
    assert any(finding["reason"] == "evidence_missing" for finding in status["findings"])


def test_assess_only_primary_degradation_skips_collector_and_renders_incomplete(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(
        monkeypatch,
        tmp_path,
        capsys,
        primary_degradation=True,
        exit_codes={"assess_estate.py": 3},
    )

    assert frontdoor.main(["--assess-only"]) == 3

    run = _assessment_run(pipeline)
    assert [Path(command[3]).name for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
        "assessment_report.py",
    ]
    report = (run / "assessment" / "assessment-report.html").read_text(encoding="utf-8")
    assert "INCOMPLETE" in report
    assert 'data-section="3" data-state="not_collected" data-reason="degraded_primary"' in report
    status, code = run_status.build_status(run)
    assert code == 1
    assert [finding["reason"] for finding in status["findings"]] == ["assessment_degraded_primary"]


def test_assess_only_missing_survey_luid_refuses_before_assessor_and_report(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, missing_survey_luid=True)

    assert frontdoor.main(["--assess-only"]) == 3

    run = _assessment_run(pipeline)
    assert [Path(command[3]).name for command in pipeline.commands] == ["run_engine_survey.py"]
    assert not (run / "assessment" / "assessment-report.html").exists()
    status, code = run_status.build_status(run)
    assert code == 1
    assert status["assessment"]["assessment"] == "missing"


@pytest.mark.parametrize(
    "pipeline_options",
    [
        {"malformed_survey": True},
        {"survey": _survey(empty=True)},
    ],
)
def test_assess_only_invalid_or_empty_survey_stops_before_assessment_and_report(
    monkeypatch, tmp_path, capsys, pipeline_options
):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, **pipeline_options)

    assert frontdoor.main(["--assess-only"]) == 3

    run = _assessment_run(pipeline)
    assert [Path(command[3]).name for command in pipeline.commands] == ["run_engine_survey.py"]
    assert not (run / "assessment" / "assessment-report.html").exists()


def test_assess_only_inconsistent_assessment_evidence_stops_before_collector_and_report(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, inconsistent_assessment=True)

    assert frontdoor.main(["--assess-only"]) == 3

    run = _assessment_run(pipeline)
    assert [Path(command[3]).name for command in pipeline.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
    ]
    assert not (run / "assessment" / "assessment-report.html").exists()
    status, code = run_status.build_status(run)
    assert code == 1
    assert any(row["reason"] == "evidence_unrecognized" for row in status["findings"])


def test_assess_only_allows_empty_project_luid_and_start_still_rejects_it(monkeypatch, tmp_path, capsys):
    assessment = _Pipeline(monkeypatch, tmp_path, capsys, empty_project_luid=True)
    assert frontdoor.main(["--assess-only"]) == 0
    assert (_assessment_run(assessment) / "assessment" / "assessment-report.html").is_file()

    migration_root = tmp_path / "migration"
    migration_root.mkdir()
    migration = _Pipeline(monkeypatch, migration_root, capsys, empty_project_luid=True)
    assert frontdoor.main([]) == 3
    assert [Path(command[3]).name for command in migration.commands] == [
        "run_engine_survey.py",
        "assess_estate.py",
    ]


def test_assess_only_default_site_note_precedes_allocation(monkeypatch, tmp_path, capsys):
    env = {**ENV, "TABLEAU_SITE": ""}
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, env_values=env)

    assert frontdoor.main(["--assess-only"]) == 0

    _out, err = _captured(capsys, pipeline)
    note = "NOTE: TABLEAU_SITE is empty, so the Default site of a Tableau Server is used."
    assert note in err
    assert err.index(note) < err.index("producer-stderr:run_engine_survey.py")
    assert "synthetic-secret" not in err


def test_assess_only_repeats_default_site_note_after_failed_survey(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(
        monkeypatch,
        tmp_path,
        capsys,
        env_values={**ENV, "TABLEAU_SITE": ""},
        exit_codes={"run_engine_survey.py": 7},
    )

    assert frontdoor.main(["--assess-only"]) == 1

    out, err = _captured(capsys, pipeline)
    note = "NOTE: TABLEAU_SITE is empty, so the Default site of a Tableau Server is used."
    assert err.count(note) == 2
    assert "ASSESSMENT FAILED (survey_failed)" in out


def test_assess_only_repeats_default_site_note_after_degraded_assessment(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(
        monkeypatch,
        tmp_path,
        capsys,
        env_values={**ENV, "TABLEAU_SITE": ""},
        primary_degradation=True,
        exit_codes={"assess_estate.py": 3},
    )

    assert frontdoor.main(["--assess-only"]) == 3

    _out, err = _captured(capsys, pipeline)
    note = "NOTE: TABLEAU_SITE is empty, so the Default site of a Tableau Server is used."
    assert err.count(note) == 2


def test_assess_only_stage_four_parent_reporting_failure_keeps_current_status(monkeypatch, tmp_path, capsys):
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    original = frontdoor._finish_stage

    def fail_report_output(result, index, state, detail="", *, total=5):
        if result.name == "report":
            raise BrokenPipeError("synthetic parent output failure")
        original(result, index, state, detail, total=total)

    monkeypatch.setattr(frontdoor, "_finish_stage", fail_report_output)

    assert frontdoor.main(["--assess-only"]) == 1

    run = _assessment_run(pipeline)
    assert (run / "assessment" / "assessment-report.html").is_file()
    status, code = run_status.build_status(run)
    assert code == 0
    assert status["assessment"]["report"] == "current"


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

    code = frontdoor.main(["--storage-decision", "policy.json"])

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
    default_env = frontdoor.build_parser().parse_args([]).env.expanduser().resolve()
    # The engine child runs at the toolkit root, so a relative policy is recorded there, not at the caller cwd.
    assert manifest.get("front_door") == {
        "env": str(default_env),
        "storage_decision": str((pipeline.root / "policy.json").resolve()),
    }
    assert "stages" not in manifest and "preparation" not in manifest
    assert "BUNDLE + REFERENCES CAPTURED" in out
    assert f"BUNDLE {run / 'bundle'} (present)" in out
    assert f"REFERENCES {run / 'oracle'} (present)" in out
    assert "fidelity NOT VERIFIED" in out
    assert not (run / "assessment" / "assessment-report.html").exists()


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


@pytest.mark.parametrize(
    "codes,expected",
    [
        ({}, 0),
        ({"run_engine_survey.py": 7}, 1),
        ({"capture_tableau_oracle.py": 4}, 1),
        ({"run_estate.py": 10}, 1),
    ],
)
def test_optional_observer_preserves_direct_frontdoor(monkeypatch, tmp_path, capsys, codes, expected):
    """An observer changes no producer command, wrapper result or reference continuation."""
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys, exit_codes=codes)
    records = []
    assert frontdoor.main(["--project", "Finance"], observer=records.append) == expected
    assert records[0]["event"] == "allocated"
    assert Path(records[0]["run"]).is_absolute()
    stages = [row for row in records if row["event"] == "stage"]
    assert stages[0] == {
        "event": "stage",
        "stage": "preflight",
        "native_exit_code": 0,
        "state": "ok",
        "artifact": None,
    }
    if "run_engine_survey.py" in codes:
        assert [row["stage"] for row in stages] == ["preflight", "survey"]
    else:
        assert stages[-1]["stage"] == "bundle"
    assert all(not Path(row["artifact"]).is_absolute() for row in stages if row["artifact"])
    assert len(pipeline.commands) == len(stages) - 1


def test_optional_diagnostic_env_blocks_before_allocation(monkeypatch, tmp_path, capsys):
    """Strict pilot metadata is opt-in; legacy permissive env behavior stays the default."""
    pipeline = _Pipeline(monkeypatch, tmp_path, capsys)
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("TABLEAU_SERVER", raising=False)
    monkeypatch.delenv("TABLEAU_PAT_VALUE", raising=False)
    selected = tmp_path / "selected.env"
    selected.write_text("TABLEAU_PAT_SECRET= \t\n", encoding="utf-8")
    records = []
    assert (
        frontdoor.main(["--project", "Finance", "--env", str(selected)], observer=records.append, diagnostic_env=True)
        == 1
    )
    assert [row["event"] for row in records] == ["configuration"]
    assert not list(pipeline.root.glob("_runs/*")), "diagnostic failure must not allocate"
    assert pipeline.commands == []
