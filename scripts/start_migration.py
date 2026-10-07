"""
purpose: start one supported Tableau site/project/workbook migration through references and bundle.
usage:   python -B scripts/start_migration.py [--project NAME|LUID] [--workbook NAME|LUID]
                                               [--env PATH] [--runs-parent PATH]
                                               [--storage-decision PATH] [--session-id UUID]

This is a thin pre-bundle front door. Exit 0 means only that the supported scope has both a bundle
and Tableau references; it does not establish fidelity, START_READY, COMPLETE or deployability.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harvest_estate_assets as harvest  # noqa: E402  # pylint: disable=wrong-import-position
import work_dirs  # noqa: E402  # pylint: disable=wrong-import-position
from tableau_env import engine_child_env, require, resolve_env  # noqa: E402  # pylint: disable=wrong-import-position

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(__file__).resolve()
HEARTBEAT_SECONDS = 15.0
FRONT_DOOR_ARTIFACTS = (
    "assessment/estate_survey.json",
    "assessment/assessment.json",
    "assessment/estate.db",
    "assessment/raw/workbooks.json",
    "assets/harvested-workbook.twb",
    "assets/harvested-workbook.twbx",
    "assets/harvested-datasource.tds",
    "assets/harvested-datasource.tdsx",
    "parse-sweep.json",
    "parse-sweep.md",
    "parse-sweep-totals.json",
    "oracle/oracle-manifest.json",
    "oracle/images/reference.png",
    "oracle/data/reference.csv",
    "bundle/report.json",
    "bundle/input_manifest.json",
    "bundle/pbip/reference.json",
)
FRONT_DOOR_HINT = "Fix: use `--runs-parent <short path>` for a new run; never bypass the output guard."


class EvidenceError(ValueError):
    """Required evidence is missing, malformed, inconsistent or out of scope."""


@dataclass(frozen=True)
class StageResult:
    """Native child outcome and the artifact path the stage was expected to produce."""

    name: str
    code: int | None
    output: Path


def build_parser() -> argparse.ArgumentParser:
    """CLI surface."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", help="one exact Tableau project name or LUID")
    parser.add_argument("--workbook", help="one exact Tableau workbook name or LUID")
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env", help="Tableau environment file")
    parser.add_argument("--runs-parent", type=Path, help="parent root for _runs (default: toolkit repository)")
    parser.add_argument("--storage-decision", type=Path, help="engine-owned datasource storage policy JSON")
    parser.add_argument("--session-id", help="Copilot session UUID for run-cost attribution")
    return parser


def _read_json(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceError(f"{label} is missing or unreadable: {type(exc).__name__}") from exc


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# The shape checks intentionally mirror the survey's machine-readable completeness contract.
# pylint: disable=too-many-branches
def _survey_workbooks(path: Path, args: argparse.Namespace) -> tuple[dict, list[dict], list[str]]:
    survey = _read_json(path, "survey")
    if not isinstance(survey, dict):
        raise EvidenceError("survey must be a JSON object")
    rows = survey.get("workbooks")
    scope = survey.get("scope")
    summary = survey.get("summary")
    if not isinstance(rows, list) or not isinstance(scope, dict) or not isinstance(summary, dict):
        raise EvidenceError("survey workbooks, scope and summary must be present")
    if scope.get("unmatched") != []:
        detail = (
            "; a workbook-centric survey cannot distinguish a missing project from a datasource-only project"
            if args.project
            else ""
        )
        raise EvidenceError(f"survey scope is unmatched or its unmatched evidence is unavailable{detail}")
    selected = scope.get("workbooks_selected")
    total = summary.get("workbooks_total")
    scoped = scope.get("scoped")
    if not _count(selected) or selected != len(rows) or not _count(total) or total != len(rows):
        raise EvidenceError("survey scope counts do not reconcile")
    if not isinstance(scoped, bool) or summary.get("scoped") is not scoped:
        raise EvidenceError("survey scoped flag is missing or inconsistent")
    if any(
        not isinstance(values, list) or any(not isinstance(value, str) or not value for value in values)
        for values in (scope.get("projects"), scope.get("workbooks"))
    ):
        raise EvidenceError("survey selector evidence is unavailable")
    if not rows:
        detail = (
            "; a workbook-centric survey cannot distinguish a missing project from a datasource-only project"
            if args.project
            else ""
        )
        raise EvidenceError(f"survey scope is empty{detail}")
    if bool(args.project or args.workbook) != scoped:
        raise EvidenceError("survey scope does not match the requested selectors")
    if not args.project and not args.workbook:
        on_site = scope.get("workbooks_on_site")
        if not _count(on_site) or on_site != len(rows):
            raise EvidenceError("site-wide survey completeness is unavailable")
    ids: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("luid"), str) or not row["luid"]:
            raise EvidenceError("survey contains a workbook without an established LUID")
        ids.append(row["luid"])
    if len(set(ids)) != len(ids):
        raise EvidenceError("survey contains duplicate workbook LUIDs")
    sources = survey.get("required_datasources")
    if not isinstance(sources, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("luid"), str) or not row["luid"] for row in sources
    ):
        raise EvidenceError("survey required_datasources evidence is unavailable")
    return survey, rows, ids


def _catalog(database: Path) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    try:
        connection = _read_only_database(database)
        try:
            projects = list(connection.execute("SELECT luid, name FROM project"))
            workbooks = list(connection.execute("SELECT luid, name, project_luid FROM workbook"))
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        raise EvidenceError(f"assessment catalog is unavailable: {type(exc).__name__}") from exc
    if any(not all(isinstance(value, str) and value for value in row) for row in projects + workbooks):
        raise EvidenceError("assessment catalog contains incomplete project/workbook identities")
    return projects, workbooks


def _resolve_named(token: str, rows: list[tuple[str, ...]], label: str, *, name_index: int) -> tuple[str, ...]:
    luid_matches = [row for row in rows if row[0] == token]
    matches = luid_matches or [row for row in rows if row[name_index].casefold() == token.casefold()]
    if not matches:
        raise EvidenceError(f"requested {label} did not match the assessment catalog")
    if len(matches) != 1:
        raise EvidenceError(f"requested {label} name is ambiguous; use its LUID")
    return matches[0]


def _resolve_selection(
    database: Path, survey_ids: list[str], args: argparse.Namespace
) -> tuple[list[str], str | None, str | None]:
    projects, workbooks = _catalog(database)
    catalog_ids = {row[0] for row in workbooks}
    if not set(survey_ids).issubset(catalog_ids):
        raise EvidenceError("survey workbook LUIDs do not join to the assessment catalog")
    project_luid = None
    if args.project:
        project_luid = _resolve_named(args.project, projects, "project", name_index=1)[0]
    requested_workbook = None
    if args.workbook:
        requested_workbook = _resolve_named(args.workbook, workbooks, "workbook", name_index=1)
        if requested_workbook[0] not in survey_ids:
            raise EvidenceError("requested workbook is absent from the surveyed scope")
        if project_luid and requested_workbook[2] != project_luid:
            raise EvidenceError("requested workbook is outside the selected project")
    if project_luid and not args.workbook:
        project_workbooks = [row[0] for row in workbooks if row[2] == project_luid]
        if not project_workbooks:
            raise EvidenceError("selected project has no workbooks; datasource-only projects are not supported")
        if not set(project_workbooks).issubset(survey_ids):
            raise EvidenceError("survey does not establish every workbook in the selected project")
        selected = project_workbooks
    elif requested_workbook:
        selected = [requested_workbook[0]]
    else:
        selected = survey_ids
    if not selected:
        raise EvidenceError("requested scope contains no workbooks")
    return selected, project_luid, requested_workbook[0] if requested_workbook else None


def _expected_harvest(database: Path, project_luid: str | None, workbook_luid: str | None) -> set[tuple[str, str]]:
    try:
        connection = _read_only_database(database)
        try:
            if workbook_luid:
                args = (connection, [], [], False, [workbook_luid])
            elif project_luid:
                args = (connection, [], [project_luid], False)
            else:
                args = (connection, [], [], False)
            todo, _projects, _workbooks, _in_project, _pulled = harvest.scoped_todo(*args)
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise EvidenceError(f"assessment harvest scope is unavailable: {type(exc).__name__}") from exc
    return {(kind, luid) for kind, luid, _name in todo}


def _read_only_database(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise EvidenceError("assessment database is absent")
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _verify_harvest(path: Path, totals_path: Path, expected: set[tuple[str, str]]) -> dict:
    rows = _read_json(path, "harvest parse-sweep")
    totals = _read_json(totals_path, "harvest totals")
    if not isinstance(rows, list) or not isinstance(totals, dict):
        raise EvidenceError("harvest artifacts have the wrong shape")
    identities = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in {"workbook", "datasource"}:
            raise EvidenceError("harvest contains an asset without an established identity")
        luid = row.get("luid")
        if not isinstance(luid, str) or not luid:
            raise EvidenceError("harvest contains an asset without an established LUID")
        identities.append((row["kind"], luid))
    if len(set(identities)) != len(identities) or set(identities) != expected:
        raise EvidenceError("harvest asset identities do not equal the requested scope")
    keys = ("both_ok", "ours_only", "theirs_only", "both_fail", "invalid", "never_downloaded")
    if (
        not _count(totals.get("total"))
        or totals["total"] != len(rows)
        or any(not _count(totals.get(key)) for key in keys)
    ):
        raise EvidenceError("harvest totals are incomplete or inconsistent")
    if sum(totals[key] for key in keys) != totals["total"]:
        raise EvidenceError("harvest outcome totals do not close")
    return totals


def _verify_oracle(path: Path, expected_workbooks: list[str]) -> dict:
    manifest = _read_json(path, "oracle manifest")
    if not isinstance(manifest, dict) or manifest.get("schema") != "tableau-oracle/1":
        raise EvidenceError("oracle manifest schema is unavailable")
    views = manifest.get("views")
    if (
        not isinstance(views, list)
        or not _count(manifest.get("view_count"))
        or manifest["view_count"] != len(views)
        or not _count(manifest.get("captured_complete"))
        or manifest["captured_complete"] != len(views)
    ):
        raise EvidenceError("oracle manifest view coverage is unavailable")
    coverage = set()
    for view in views:
        if not isinstance(view, dict) or not isinstance(view.get("workbook_luid"), str) or not view["workbook_luid"]:
            raise EvidenceError("oracle manifest has a view without an established workbook LUID")
        coverage.add(view["workbook_luid"])
    if not set(expected_workbooks).issubset(coverage) or not coverage.issubset(set(expected_workbooks)):
        raise EvidenceError("oracle workbook coverage does not equal the requested scope")
    return manifest


def _verify_bundle(bundle: Path) -> dict:
    report = _read_json(bundle / "report.json", "bundle report")
    try:
        manifest = _read_json(bundle / "input_manifest.json", "bundle input manifest")
    except EvidenceError as exc:
        raise EvidenceError("bundle input_manifest.scope_bridge is missing or unreadable") from exc
    bridge = manifest.get("scope_bridge") if isinstance(manifest, dict) else None
    if not isinstance(report, dict) or not isinstance(bridge, dict) or bridge.get("status") != "established":
        raise EvidenceError("bundle scope_bridge is missing or not established")
    if not isinstance(report.get("workbooks"), list) or not isinstance(report.get("datasources"), list):
        raise EvidenceError("bundle report counts are unavailable")
    return report


def _stop_process(process: subprocess.Popen) -> None:
    """Stop only this invocation's child process group and reap its direct child."""
    try:
        if os.name == "nt":
            process.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _run_child(name: str, index: int, command: list[str], env: dict[str, str], output: Path) -> StageResult:
    print(f"[{index}/5] {name} - started {time.strftime('%H:%M:%S')}", flush=True)
    options: dict[str, Any] = {"cwd": str(REPO_ROOT), "env": env, "stdout": None, "stderr": None}
    if os.name == "nt":
        options["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        options["start_new_session"] = True
    try:
        process_context = subprocess.Popen(command, **options)
    except OSError as exc:
        print(f"[{index}/5] {name} - FAILED exit=launch-failed out={output}", flush=True)
        print(f"Could not start {name}: {type(exc).__name__}", file=sys.stderr, flush=True)
        return StageResult(name, None, output)
    with process_context as process:
        started = time.monotonic()
        heartbeat = started + HEARTBEAT_SECONDS
        try:
            while process.poll() is None:
                now = time.monotonic()
                if now >= heartbeat:
                    print(f"[{index}/5] {name} - running elapsed={int(now - started)}s", flush=True)
                    heartbeat = now + HEARTBEAT_SECONDS
                time.sleep(min(0.2, max(0.01, heartbeat - now)))
            return StageResult(name, process.wait(), output)
        except KeyboardInterrupt:
            _stop_process(process)
            raise


def _finish_stage(result: StageResult, index: int, status: str, detail: str = "") -> None:
    native = str(result.code) if result.code is not None else "launch-failed"
    suffix = f" {detail}" if detail else ""
    print(f"[{index}/5] {result.name} - {status} exit={native}{suffix} out={result.output}", flush=True)


def _preflight(env: dict[str, str]) -> int:
    command = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(REPO_ROOT / "scripts" / "preflight.ps1"),
    ]
    try:
        return subprocess.run(command, cwd=REPO_ROOT, env=env, check=False).returncode
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print(f"Migration preflight could not start: {type(exc).__name__}", file=sys.stderr, flush=True)
        return 1


def _scope_label(args: argparse.Namespace, env: dict[str, str]) -> tuple[str, str]:
    if args.workbook:
        return "workbook", args.workbook
    if args.project:
        return "project", args.project
    site = env.get("TABLEAU_SITE") or "Default"
    return "site", site


def _attribution(args: argparse.Namespace, environ: dict[str, str]) -> dict[str, str]:
    supplied = args.session_id if args.session_id is not None else environ.get("COPILOT_AGENT_SESSION_ID")
    if supplied is None:
        return {"driver": "operator"}
    try:
        session_id = str(uuid.UUID(supplied))
    except (AttributeError, ValueError) as exc:
        raise ValueError("--session-id/COPILOT_AGENT_SESSION_ID must be a UUID") from exc
    return {"driver": "copilot", "session_id": session_id}


def _front_door_command(args: argparse.Namespace, env_path: Path, short_root: Path) -> str:
    command = [sys.executable, "-B", str(SCRIPT), "--env", str(env_path), "--runs-parent", str(short_root)]
    for flag, value in (
        ("--project", args.project),
        ("--workbook", args.workbook),
        ("--storage-decision", str(args.storage_decision) if args.storage_decision else None),
        ("--session-id", args.session_id),
    ):
        if value:
            command.extend([flag, value])
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def _engine_started(bundle: Path) -> bool | None:
    if (bundle / "engine-output-receipt.json").exists():
        return True
    phase_file = bundle / "phase-timings.json"
    if not phase_file.exists():
        return False
    try:
        data = json.loads(phase_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    phases = data.get("phases") if isinstance(data, dict) else None
    if not isinstance(phases, list):
        return None
    return any(isinstance(phase, dict) and phase.get("phase") == "engine_run" for phase in phases)


def _summary(run: Path, bundle: Path, oracle: Path, overall: int, *, fidelity: str) -> None:
    print("NEXT STEPS", flush=True)
    if overall == 0:
        print("1. Compare the working/shipping report with Tableau before calling the migration done.", flush=True)
    else:
        print(
            "1. Remedy the reported blocking input or reference-capture issue, then inspect retained artifacts.",
            flush=True,
        )
    handover = bundle / "handover"
    for label, path in (
        ("BUNDLE", bundle),
        ("HANDOVER", handover),
        ("REFERENCES", oracle),
    ):
        state = "present" if path.exists() else "absent"
        print(f"{label} {path} ({state})", flush=True)
    print(
        "2. Give the exact handover and reference paths to the root dispatcher for an issued brief and "
        "@tableau-migrator handoff; package/cohort readiness remains separate.",
        flush=True,
    )
    phrase = (
        "BUNDLE + REFERENCES CAPTURED; fidelity NOT VERIFIED; not START_READY, COMPLETE or deployable."
        if overall == 0
        else f"Migration front door did not establish success; {fidelity}."
    )
    print(f"Overall exit={overall}. {phrase}", flush=True)
    print(f"RUN {run}", flush=True)


# This entry point is intentionally the five-stage composition; stage contracts stay adjacent here.
# pylint: disable=too-many-locals,too-many-return-statements,too-many-branches,too-many-statements
def main(argv: list[str] | None = None) -> int:
    """Run survey, assessment, harvest, reference capture and deterministic conversion."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.project is not None and not args.project:
        parser.error("--project must not be empty")
    if args.workbook is not None and not args.workbook:
        parser.error("--workbook must not be empty")
    try:
        attribution = _attribution(args, os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    env_path = args.env.expanduser().resolve()
    parent = args.runs_parent.expanduser().resolve() if args.runs_parent else None
    candidate = work_dirs.runs_root(parent) / "000-front-door-preflight"
    if harvest.refuse_unignored_output(
        candidate,
        False,
        artifacts=FRONT_DOOR_ARTIFACTS,
        hint=FRONT_DOOR_HINT,
    ):
        print(f"Output guard refused before allocation. {FRONT_DOOR_HINT}", file=sys.stderr, flush=True)
        return 1
    try:
        env = resolve_env(env_path)
        require(env)
    except (OSError, ValueError, SystemExit) as exc:
        print(f"Cannot establish Tableau environment: {exc}", file=sys.stderr, flush=True)
        return 1
    kind, display = _scope_label(args, env)
    metadata = {
        "scope": {
            "kind": kind,
            "display": display,
            "server": env.get("TABLEAU_SERVER_URL", ""),
            "site": env.get("TABLEAU_SITE", ""),
            "selection": {"project": args.project, "workbook": args.workbook},
        },
        "assessment_scope": "site",
        "attribution": attribution,
    }
    try:
        run = work_dirs.allocate_run(display, repo_root=parent, extra_manifest=metadata)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Run allocation refused: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1
    root = run.root.resolve()
    assessment = root / "assessment"
    survey_path = assessment / "estate_survey.json"
    asset_root = root
    oracle_root = root / "oracle"
    bundle = root / "bundle"
    print(root, flush=True)
    child_env = engine_child_env(env)
    child_env["PYTHONUNBUFFERED"] = "1"
    common_python = [sys.executable, "-u", "-B"]
    failed = False
    invalid = False
    fidelity = "fidelity NOT VERIFIED"
    try:
        preflight_code = _preflight(child_env)
        if preflight_code == 130:
            print("Migration interrupted during preflight.", flush=True)
            return _finalize(root, bundle, oracle_root, True, False, fidelity, code=130)
        if preflight_code != 0:
            print(f"Migration preflight FAILED exit={preflight_code}", flush=True)
            failed = True
            return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)

        survey_command = [
            *common_python,
            str(REPO_ROOT / "scripts" / "run_engine_survey.py"),
            "--server",
            env["TABLEAU_SERVER_URL"],
            "--site",
            env.get("TABLEAU_SITE", ""),
            "--pat-name",
            env["TABLEAU_PAT_NAME"],
            "--env-file",
            str(env_path),
            "--json",
            str(survey_path),
        ]
        if args.project:
            survey_command.extend(["--project", args.project])
        if args.workbook:
            survey_command.extend(["--workbook", args.workbook])
        result = _run_child("survey", 1, survey_command, child_env, survey_path)
        if result.code != 0:
            _finish_stage(result, 1, "FAILED")
            failed = True
            return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)
        try:
            survey, _survey_rows, survey_ids = _survey_workbooks(survey_path, args)
            survey_counts = f"workbooks={len(survey_ids)} required_sources={len(survey['required_datasources'])}"
        except EvidenceError as exc:
            _finish_stage(result, 1, "CANNOT_ESTABLISH", str(exc))
            invalid = True
            return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)
        _finish_stage(result, 1, "OK", survey_counts)

        assess_command = [
            *common_python,
            str(REPO_ROOT / "scripts" / "assess_estate.py"),
            "--env",
            str(env_path),
            "--out",
            str(assessment),
            "--survey",
            str(survey_path),
        ]
        result = _run_child("assessment", 2, assess_command, child_env, assessment)
        if result.code != 0:
            _finish_stage(result, 2, "FAILED")
            failed = True
            return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)
        try:
            assessment_doc = _read_json(assessment / "assessment.json", "assessment")
            if not isinstance(assessment_doc, dict) or not (assessment / "estate.db").is_file():
                raise EvidenceError("assessment JSON or database is absent")
            selected_workbooks, project_luid, workbook_luid = _resolve_selection(
                assessment / "estate.db", survey_ids, args
            )
            db_counts = (
                f"workbooks={len(assessment_doc['workbooks'])}"
                if isinstance(assessment_doc.get("workbooks"), list)
                else "workbooks=unavailable"
            )
        except (EvidenceError, KeyError) as exc:
            _finish_stage(result, 2, "CANNOT_ESTABLISH", str(exc))
            invalid = True
            return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)
        _finish_stage(result, 2, "OK", db_counts)

        harvest_command = [
            *common_python,
            str(REPO_ROOT / "scripts" / "harvest_estate_assets.py"),
            "--env",
            str(env_path),
            "--out",
            str(asset_root),
            "--db",
            str(assessment / "estate.db"),
        ]
        if workbook_luid:
            harvest_command.extend(["--workbook-id", workbook_luid])
        elif project_luid:
            harvest_command.extend(["--project-id", project_luid])
        result = _run_child("harvest", 3, harvest_command, child_env, asset_root / "parse-sweep.json")
        if result.code != 0:
            _finish_stage(result, 3, "FAILED")
            failed = True
            return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)
        try:
            expected = _expected_harvest(assessment / "estate.db", project_luid, workbook_luid)
            totals = _verify_harvest(asset_root / "parse-sweep.json", asset_root / "parse-sweep-totals.json", expected)
        except EvidenceError as exc:
            _finish_stage(result, 3, "CANNOT_ESTABLISH", str(exc))
            invalid = True
            return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)
        _finish_stage(result, 3, "OK", f"assets={totals['total']}")

        oracle_command = [
            *common_python,
            str(REPO_ROOT / "scripts" / "capture_tableau_oracle.py"),
            "--env",
            str(env_path),
            "--out",
            str(oracle_root),
            "--images",
            "--reference-best",
        ]
        for luid in selected_workbooks:
            oracle_command.extend(["--workbook-id", luid])
        result = _run_child("reference", 4, oracle_command, child_env, oracle_root / "oracle-manifest.json")
        if result.code == 0:
            try:
                manifest = _verify_oracle(oracle_root / "oracle-manifest.json", selected_workbooks)
                _finish_stage(result, 4, "OK", f"views={manifest['view_count']}")
            except EvidenceError as exc:
                _finish_stage(result, 4, "CANNOT_ESTABLISH", str(exc))
                invalid = True
        else:
            _finish_stage(result, 4, "FAILED")
            failed = True

        bundle_command = [
            *common_python,
            str(REPO_ROOT / "scripts" / "run_estate.py"),
            "--input",
            str(root / "assets"),
            "--output",
            str(bundle),
            "--scope-survey",
            str(survey_path),
        ]
        if args.storage_decision:
            bundle_command.extend(["--storage-decision", str(args.storage_decision)])
        result = _run_child("bundle", 5, bundle_command, child_env, bundle / "report.json")
        if result.code == 10:
            engine_state = _engine_started(bundle)
            if engine_state is False:
                print(
                    "PATH CEILING: stopped before conversion; allocated run and harvested inputs retained.", flush=True
                )
            elif engine_state is True:
                print("PATH CEILING: bundle was built and retained.", flush=True)
            else:
                print(
                    "PATH CEILING: engine phase cannot be established; retained output is not classified.", flush=True
                )
                invalid = True
            short_root = Path("C:/t2p")
            print(
                "Rerun in a new run (old run retained): " + _front_door_command(args, env_path, short_root),
                flush=True,
            )
            _finish_stage(result, 5, "FAILED")
            failed = True
        elif result.code != 0:
            _finish_stage(result, 5, "FAILED")
            failed = True
        else:
            try:
                report = _verify_bundle(bundle)
                _finish_stage(
                    result,
                    5,
                    "OK",
                    f"workbooks={len(report['workbooks'])} datasources={len(report['datasources'])}",
                )
            except EvidenceError as exc:
                _finish_stage(result, 5, "CANNOT_ESTABLISH", str(exc))
                invalid = True
    except KeyboardInterrupt:
        print("Migration interrupted; invocation-owned child work was cancelled and reaped.", flush=True)
        return _finalize(root, bundle, oracle_root, True, False, fidelity, code=130)
    return _finalize(root, bundle, oracle_root, failed, invalid, fidelity)


# The separate flags encode wrapper precedence without conflating invalid evidence and failed stages.
# pylint: disable=too-many-arguments,too-many-positional-arguments
def _finalize(
    run: Path,
    bundle: Path,
    oracle: Path,
    failed: bool,
    invalid: bool,
    fidelity: str,
    *,
    code: int | None = None,
) -> int:
    overall = code if code is not None else (3 if invalid else 1 if failed else 0)
    _summary(run, bundle, oracle, overall, fidelity=fidelity)
    return overall


if __name__ == "__main__":
    raise SystemExit(main())
