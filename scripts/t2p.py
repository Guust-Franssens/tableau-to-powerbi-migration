"""
purpose: narrow doctor/start/status pilot delegating to existing configuration and run owners.
usage:   python -B scripts/t2p.py {doctor,start,status} [args]

No readiness, liveness, fidelity, credential validity or completion certification.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_VERSION = "t2p/v1"
STAGE_OWNERS = {
    "preflight": "preflight.ps1",
    "survey": "run_engine_survey.py",
    "assessment": "assess_estate.py",
    "harvest": "harvest_estate_assets.py",
    "reference": "capture_tableau_oracle.py",
    "bundle": "run_estate.py",
}
ENVELOPE_SCHEMA = {
    "type": "object",
    "required": [
        "schema_version",
        "command",
        "status",
        "exit_code",
        "authority",
        "delegated_exit_code",
        "stages",
        "checks",
        "evidence",
        "artifacts",
        "run_location",
        "runs_root",
        "path_report",
        "limitations",
        "proof_boundary",
    ],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "command": {"enum": ["doctor", "start", "status", None]},
        "status": {
            "enum": [
                "ok",
                "findings",
                "cannot_establish",
                "error",
                "interrupted",
                "READY_TO_ATTEMPT",
                "BLOCKED_CONFIG",
                "CANNOT_ESTABLISH",
            ]
        },
        "exit_code": {"type": "integer"},
        "authority": {"type": ["string", "null"]},
        "delegated_exit_code": {"type": ["integer", "null"]},
        "stages": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["stage", "owner", "native_exit_code", "state", "artifact"],
                "properties": {
                    "stage": {"enum": list(STAGE_OWNERS)},
                    "owner": {"type": "string"},
                    "native_exit_code": {"type": ["integer", "null"]},
                    "state": {"type": "string"},
                    "artifact": {"type": ["string", "null"]},
                },
            },
        },
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "state", "source", "required", "secret", "reason", "remedy"],
                "properties": {
                    "key": {"type": "string", "pattern": "^TABLEAU_"},
                    "state": {"enum": ["set", "missing", "blank", "conflict", "deprecated-alias"]},
                    "source": {"enum": ["environment", "selected_env", "default", "absent"]},
                    "required": {"type": "boolean"},
                    "secret": {"type": "boolean"},
                    "reason": {"type": "string"},
                    "remedy": {"type": "string"},
                },
            },
        },
        "evidence": {"type": "array", "items": {"type": "object", "required": ["owner", "locator"]}},
        "artifacts": {"type": "array", "items": {"type": "object", "required": ["owner", "locator"]}},
        "run_location": {"type": ["object", "null"]},
        "runs_root": {"type": "null"},
        "path_report": {"type": ["object", "null"]},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "proof_boundary": {
            "type": "object",
            "required": ["established", "not_established"],
            "properties": {
                "established": {"type": "array", "items": {"type": "string"}},
                "not_established": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}


class ArgumentError(ValueError):
    """A syntax failure without reflecting arbitrary user input."""


class Parser(argparse.ArgumentParser):
    """Do not echo argv, which can contain secrets."""

    def error(self, message: str) -> None:
        del message
        raise ArgumentError("invalid_arguments_use_help")


def build_parser() -> argparse.ArgumentParser:
    """The entire supported command tree; no implicit input routing."""
    parser = Parser(
        prog="python -B scripts/t2p.py",
        allow_abbrev=False,
        description="Narrow pilot: diagnostic doctor, live project start, read-only status. Not readiness.",
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for command, help_text in (
        ("doctor", "Read-only plain preflight and configuration presence; no login or run."),
        ("start", "Attempt one exact live Tableau project; assessment remains site-wide."),
        ("status", "Read-only inventory for one absolute run; no next action or readiness."),
    ):
        child = commands.add_parser(command, help=help_text, description=help_text, allow_abbrev=False)
        child.add_argument("--json", action="store_true", help="one t2p/v1 JSON envelope on stdout")
        if command in {"doctor", "start"}:
            child.add_argument("--env", type=Path, help="selected environment file (default: toolkit .env)")
        if command == "start":
            child.add_argument("--project", required=True, help="one nonempty exact project name or LUID")
            child.add_argument("--runs-parent", type=Path, help="explicit parent for _runs; no relocation")
            child.add_argument("--storage-decision", type=Path, help="existing engine-owned storage policy")
            child.add_argument("--session-id", help="existing Copilot session UUID attribution")
        if command == "status":
            child.add_argument("--run", required=True, help="absolute local physical run; never latest")
    return parser


def envelope(command: str | None) -> dict[str, Any]:
    """Stable nullable keys also appear in syntax, launch and interrupt results."""
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": "error",
        "exit_code": 1,
        "authority": None,
        "delegated_exit_code": None,
        "stages": [],
        "checks": [],
        "evidence": [],
        "artifacts": [],
        "run_location": None,
        "runs_root": None,
        "run_location_reason": "not_applicable_no_run" if command == "doctor" else "not_allocated_path_withheld",
        "path_report": None if command == "doctor" else path_report(command),
        "limitations": [],
        "proof_boundary": {
            "established": [],
            "not_established": [
                "credential_validity",
                "source_reachability",
                "path_safety",
                "process_liveness",
                "START_READY",
                "fidelity",
                "COMPLETE",
                "deployability",
            ],
        },
    }


def path_report(command: str) -> dict:
    """Unrecorded measurements are not guessed or recomputed by the facade."""
    reason = "unknown_until_harvest" if command == "start" else "not_recorded"
    return {
        "owner": "run_estate.py",
        "initial_decision": "cannot_establish",
        "initial_reason": "unknown_until_harvest",
        "file_ceiling_utf16": 259,
        "directory_ceiling_utf16": 247,
        "projection_verdict": None,
        "projection_reason": reason,
        **{
            field: {"value": None, "reason": reason}
            for field in (
                "longest_file_locator",
                "longest_directory_locator",
                "longest_file_utf16",
                "longest_directory_utf16",
                "remaining_file_utf16",
                "remaining_directory_utf16",
            )
        },
        "rerun": {"argv": None, "authoritative": False, "reason": "arguments_not_recorded"},
        "subst": {"value": None, "reason": "not_derivable_read_only"},
    }


def _stage(name: str, code: int | None, state: str, artifact: str | None = None) -> dict:
    return {
        "stage": name,
        "owner": STAGE_OWNERS[name],
        "native_exit_code": code,
        "state": state,
        "artifact": artifact,
    }


def doctor(args: argparse.Namespace, result: dict) -> int:
    """Capture plain diagnostics privately; emit only owner-generated presence records."""
    from tableau_env import diagnose_env  # pylint: disable=import-outside-toplevel

    result["authority"] = "preflight.ps1+tableau_env.py"
    _values, configuration = diagnose_env(args.env or ROOT / ".env", explicit=args.env is not None)
    result["checks"] = configuration["checks"]
    result["limitations"] = [configuration["reason"], "presence_only_not_authentication"]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    print("DOCTOR plain preflight; output withheld; no login or run.", file=sys.stderr, flush=True)
    try:
        child = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "preflight.ps1")],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        code = child.returncode
    except OSError:
        code = None
    result["stages"] = [
        _stage("preflight", code, "ok" if code == 0 else "cannot_establish" if code is None else "failed")
    ]
    unknown = configuration["status"] == "CANNOT_ESTABLISH" or code not in {0, 1}
    blocked = configuration["status"] == "BLOCKED_CONFIG" or code == 1
    result["status"] = "CANNOT_ESTABLISH" if unknown else "BLOCKED_CONFIG" if blocked else "READY_TO_ATTEMPT"
    result["proof_boundary"]["established"] = ["configuration_presence", "plain_preflight_result"]
    return 3 if unknown else 1 if blocked else 0


@contextmanager
def _native_to_stderr() -> Iterator[None]:
    """Redirect both Python prints and inherited OS descriptors, without buffering children."""
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        with redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stderr.flush()
        os.dup2(saved, 1)
        os.close(saved)


def _run_line(result: dict, args: argparse.Namespace) -> str:
    location = result["run_location"]
    if location is not None:
        return "RUN " + location["physical_run"]
    from work_dirs import runs_root  # pylint: disable=import-outside-toplevel

    parent = args.runs_parent.expanduser().resolve() if args.runs_parent is not None else None
    return f"RUN NOT_ALLOCATED; runs_root={runs_root(parent)}"


def start(args: argparse.Namespace, result: dict) -> int:
    """One delegated front door; observer records outcomes, never parses child prose."""
    import start_migration  # pylint: disable=import-outside-toplevel

    result["authority"] = "start_migration.py"
    result["stages"] = [_stage(name, None, "not_launched") for name in STAGE_OWNERS]
    delegated = ["--project", args.project]
    for flag, value in (
        ("--env", args.env),
        ("--runs-parent", args.runs_parent),
        ("--storage-decision", args.storage_decision),
        ("--session-id", args.session_id),
    ):
        if value is not None:
            delegated.extend([flag, str(value)])

    def observe(record: dict) -> None:
        event = record["event"]
        if event == "allocated":
            run = record["run"]
            from work_dirs import runs_root  # pylint: disable=import-outside-toplevel

            result["run_location"] = {
                "recorded_run": run,
                "selected_run": run,
                "physical_run": run,
                "relationship": "repo_local" if Path(run).parent == runs_root().resolve() else "external",
                "git_visibility": "unknown",
                "git_visibility_reason": "not_recorded",
                "commit_safety": "not_for_git",
                "commit_safety_reason": "existing_output_guard_passed",
            }
            result["run_location_reason"] = None
            _print_safe("RUN " + run)
            _print_safe("RUNS_ROOT " + str(Path(run).parent))
        elif event == "stage":
            name = record["stage"]
            result["stages"][list(STAGE_OWNERS).index(name)] = _stage(
                name,
                record["native_exit_code"],
                record["state"],
                record["artifact"],
            )
            if record["artifact"] is not None:
                reference = {
                    "owner": STAGE_OWNERS[name],
                    "locator": record["artifact"],
                    "check": "owner_validated" if record["state"] == "ok" else "not_established",
                }
                result["evidence"].append(reference)
                result["artifacts"].append(dict(reference))
            if name == "bundle" and result["path_report"]["projection_verdict"] is None:
                result["path_report"]["projection_reason"] = "not_recorded"
        elif event == "path_gate":
            report = result["path_report"]
            report["projection_verdict"] = record["verdict"]
            report["projection_reason"] = record["reason"]
            result["evidence"].append(
                {
                    "owner": record["owner"],
                    "locator": record["evidence"],
                    "check": "owner_gate_observed",
                }
            )
            for kind in ("file", "directory"):
                for field in (f"longest_{kind}_utf16", f"remaining_{kind}_utf16", f"longest_{kind}_locator"):
                    report[field] = {"value": None, "reason": "not_recorded"}
            for kind, measurement in record["measurements"].items():
                report[f"{kind}_ceiling_utf16"] = measurement["ceiling"]
                report[f"longest_{kind}_utf16"] = {"value": measurement["length"], "reason": None}
                report[f"remaining_{kind}_utf16"] = {
                    "value": measurement["ceiling"] - measurement["length"],
                    "reason": None,
                }
                report[f"longest_{kind}_locator"] = {"value": None, "reason": "customer_label_withheld"}
        elif event == "configuration":
            result["checks"] = record["checks"]
            result["limitations"].append(record["reason"])

    _print_safe(_run_line(result, args))
    try:
        code = start_migration.main(delegated, observer=observe, diagnostic_env=True)
    except KeyboardInterrupt:
        code = 130
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    result["delegated_exit_code"] = code
    result["status"] = (
        "ok" if code == 0 else "interrupted" if code == 130 else "cannot_establish" if code == 3 else "error"
    )
    result["proof_boundary"]["established"] = ["delegated_wrapper_result"]
    result["limitations"].append("capture_only_not_completion")
    return code


def status(args: argparse.Namespace, result: dict) -> int:
    """Project safe inventory observations; never call status guidance, path scanners or git."""
    import run_status  # pylint: disable=import-outside-toplevel

    result["authority"] = "run_status.py"
    observed, code = run_status.build_status(args.run)
    result["delegated_exit_code"] = code
    intact = observed.get("run_location", {}).get("state") == "intact"
    result["status"] = "cannot_establish" if not intact else "findings" if observed["findings"] else "ok"
    if intact:
        result["run_location"] = {
            "recorded_run": args.run,
            "selected_run": args.run,
            "physical_run": args.run,
            "relationship": None,
            "relationship_reason": "not_derivable_read_only",
            "git_visibility": None,
            "git_visibility_reason": "not_derivable_read_only",
            "commit_safety": None,
            "commit_safety_reason": "not_derivable_read_only",
        }
        result["run_location_reason"] = None
    else:
        result["run_location_reason"] = "identity_unestablished"
    # Identity labels and arbitrary manifest strings are deliberately not copied. Ordered records
    # retain multiplicity, owner-derived state and fixed reason codes, not customer prose.
    result["observations"] = {
        "run_recorded_status": observed.get("run_recorded_status"),
        "inventory_scope": observed.get("inventory_scope"),
        "canonical_subdirs": observed.get("canonical_subdirs"),
        "child_evidence": observed.get("child_evidence"),
        "findings": [
            {"code": row["code"], "reason": row["reason"], "unassessable": row["unassessable"]}
            for row in observed["findings"]
        ],
        "units": [
            {
                "ordinal": index,
                "kind": row["kind"],
                "scope": row["scope"],
                "generated_working_copy": row["generated_working_copy"],
                "handover": row["handover"],
                "occurrences": [{"kind": item["kind"], "status": item["status"]} for item in row["occurrences"]],
                "package_count": int(row["package"] is not None),
                "current_certification": row["current_certification"],
            }
            for index, row in enumerate(observed.get("units", []))
        ],
        "unscoped_package_count": len(observed.get("unscoped_packages", [])),
        "recorded_phases": [
            {key: row.get(key) for key in ("source", "phase", "last_observed", "failed")}
            for row in observed.get("recorded_phases", [])
        ],
    }
    result["limitations"] = ["last_observed_not_liveness", "customer_labels_withheld", "no_routing"]
    result["proof_boundary"]["established"] = ["read_only_inventory"]
    return code


def _print_safe(text: str) -> None:
    """ASCII escapes keep human diagnostics printable even under cp1252."""
    print(text.encode("ascii", errors="backslashreplace").decode("ascii"), flush=True)


# The three explicit branches deliberately do not use a command registry/router.
# pylint: disable=too-many-branches
def main(argv: list[str] | None = None) -> int:
    """Render one versioned result without reflecting exception text or invalid argv."""
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    command = argv[0] if argv and argv[0] in {"doctor", "start", "status"} else None
    result = envelope(command)
    args = None
    try:
        args = build_parser().parse_args(argv)
        if args.command == "start" and not args.project.strip():
            raise ArgumentError("nonempty_project_required")
        if args.command == "doctor":
            code = doctor(args, result)
        elif args.command == "start":
            # The path marker is local progress, not part of the stdout JSON stream.
            if json_mode:
                with _native_to_stderr():
                    code = start(args, result)
            else:
                code = start(args, result)
        else:
            code = status(args, result)
    except ArgumentError:
        code = 2
        result["limitations"] = ["invalid_arguments_use_help"]
    except KeyboardInterrupt:
        code = 130
        result["status"] = "interrupted"
    except (OSError, ValueError, RuntimeError):
        code = 3 if command == "doctor" else 1
        result["status"] = "CANNOT_ESTABLISH" if command == "doctor" else "error"
        result["limitations"].append("owner_result_unavailable")
    result["exit_code"] = code
    if json_mode:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True), flush=True)
    else:
        _print_safe(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    if args is not None and args.command == "start":
        line = _run_line(result, args)
        if json_mode:
            print(line, file=sys.stderr, flush=True)
        else:
            _print_safe(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
