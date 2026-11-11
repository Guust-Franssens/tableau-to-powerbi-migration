"""
purpose: narrow doctor/start/status/units pilot delegating to existing configuration and run owners.
usage:   python -B scripts/t2p.py {doctor,start,assess,status,units} [args]

No readiness, liveness, fidelity, credential validity or completion certification.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, redirect_stdout
from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import tomllib
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit
from urllib.request import url2pathname

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
ASSESS_ONLY_OWNERS = {
    "admin_insights": "admin_insights_collector.py",
    "report": "assessment_report.py",
}
ALL_STAGE_OWNERS = STAGE_OWNERS | ASSESS_ONLY_OWNERS
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
        "command": {"enum": ["doctor", "start", "assess", "status", "units", None]},
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
                    "stage": {"enum": list(ALL_STAGE_OWNERS)},
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
        "unit_lookup": {
            "type": "object",
            "required": ["schema_version", "action", "result", "reason", "query", "runs_root", "candidates", "runs"],
            "properties": {
                "schema_version": {"const": "unit-lookup/v1"},
                "action": {"enum": ["list", "show", "find"]},
                "result": {"enum": ["found", "missing", "ambiguous", "unassessable", "legacy"]},
                "candidates": {"type": "array", "items": {"type": "object", "required": ["id", "state", "run"]}},
                "runs": {"type": "array", "items": {"type": "object", "required": ["run", "state"]}},
            },
        },
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


def build_parser(prog: str = "python -B scripts/t2p.py") -> argparse.ArgumentParser:
    """The entire supported command tree; no implicit input routing."""
    parser = Parser(
        prog=prog,
        allow_abbrev=False,
        description=(
            "Narrow pilot: diagnostic doctor, assessment-only survey, live project start, read-only status, "
            "read-only unit lookup."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    for command, help_text in (
        ("doctor", "Read-only plain preflight and configuration presence; no login or run."),
        ("start", "Attempt one exact live Tableau project; assessment remains site-wide."),
        ("assess", "Assess one Tableau site and write offline HTML; no harvest, capture, or conversion."),
        ("status", "Read-only inventory for one absolute run: units, path budget and the owner's next command."),
        ("units", "Read-only unit-ID lookup across one runs root; no allocation, rename or refresh."),
    ):
        child = commands.add_parser(command, help=help_text, description=help_text, allow_abbrev=False)
        if command == "units":
            _add_units_actions(child)
            continue
        child.add_argument("--json", action="store_true", help="one t2p/v1 JSON envelope on stdout")
        if command in {"doctor", "start", "assess"}:
            child.add_argument("--env", type=Path, help="selected environment file (default: toolkit .env)")
        if command == "start":
            child.add_argument("--project", required=True, help="one nonempty exact project name or LUID")
            child.add_argument("--runs-parent", type=Path, help="explicit parent for _runs; no relocation")
            child.add_argument("--storage-decision", type=Path, help="existing engine-owned storage policy")
            child.add_argument("--session-id", help="existing Copilot session UUID attribution")
        if command == "status":
            child.add_argument("--run", required=True, help="absolute local physical run; never latest")
        if command == "assess":
            child.add_argument("--runs-parent", type=Path, help="explicit parent for _runs; no relocation")
            child.add_argument("--session-id", help="existing Copilot session UUID attribution")
    return parser


def _add_units_actions(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="units_action", required=True, parser_class=Parser)
    for action, help_text in (
        ("list", "Every allocated unit under the runs root, plus legacy runs without an index."),
        ("show", "Resolve one unit ID to its owning run without naming the run."),
        ("find", "All units whose original name contains TEXT and/or whose source LUID matches."),
    ):
        child = actions.add_parser(action, help=help_text, description=help_text, allow_abbrev=False)
        child.add_argument("--json", action="store_true", help="one t2p/v1 JSON envelope on stdout")
        child.add_argument("--runs-parent", type=Path, help="explicit parent for _runs (default: toolkit)")
        if action == "show":
            child.add_argument("unit_id", metavar="ID", help="one unit ID, for example u0001")
        if action == "find":
            child.add_argument("--name", help="case-insensitive substring of the original name")
            child.add_argument("--luid", help="exact source LUID")


def _project_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _toolkit_origin(origin: dict) -> tuple[Path | None, str, int]:
    """Accept only an ordinary directory origin and finite, nonredirected toolkit paths."""
    # pylint: disable=too-many-return-statements
    if (
        set(origin) != {"url", "dir_info"}
        or not isinstance(origin.get("url"), str)
        or not isinstance(origin.get("dir_info"), dict)
        or set(origin["dir_info"]) - {"editable"}
        or origin["dir_info"].get("editable", False) is not False
    ):
        return None, "install_form_unsupported", 1
    url = urlsplit(origin["url"])
    if url.scheme != "file" or url.netloc or url.query or url.fragment or url.path.startswith("//"):
        return None, "install_form_unsupported", 1
    root = Path(url2pathname(url.path))
    if not root.is_absolute() or root.drive.startswith("\\\\"):
        return None, "install_form_unsupported", 1
    if not root.is_dir():
        return None, "install_target_unavailable", 3
    if root.resolve(strict=True) != root or ".." in root.parts:
        return None, "install_target_redirected", 1
    required = [
        root / "pyproject.toml",
        *(
            root / "scripts" / name
            for name in (
                "t2p.py",
                "start_migration.py",
                "run_status.py",
                "tableau_env.py",
                "work_dirs.py",
                "preflight.ps1",
            )
        ),
    ]
    for path in required:
        if not path.is_file():
            return None, "install_target_incomplete", 3
        if path.resolve(strict=True) != path:
            return None, "install_target_redirected", 1
    project = tomllib.loads(required[0].read_text(encoding="utf-8")).get("project", {})
    if not isinstance(project, dict) or project.get("name") != "tableau-to-pbi-migration":
        return None, "install_target_identity_mismatch", 1
    return root, "", 0


def _installed_root() -> tuple[Path | None, str, int]:
    """Use only this interpreter's installation sites, never cwd distribution discovery."""
    # pylint: disable=too-many-return-statements
    try:
        sites = list(dict.fromkeys(Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")))
        distributions = list(metadata.distributions(path=[str(site) for site in sites]))
        matches = [
            dist for dist in distributions if _project_name(dist.metadata.get("Name", "")) == "tableau-to-pbi-migration"
        ]
        if len(matches) != 1:
            reason = "install_distribution_ambiguous" if matches else "install_distribution_unavailable"
            return None, reason, 3
        providers = [
            (dist, entry)
            for dist in distributions
            for entry in dist.entry_points
            if entry.group == "console_scripts" and entry.name == "t2p"
        ]
        if len(providers) != 1:
            reason = "install_provider_ambiguous" if providers else "install_provider_unavailable"
            return None, reason, 3
        dist = matches[0]
        provider, entry = providers[0]
        if provider is not dist or entry.value != "t2p:console_main":
            return None, "install_provider_mismatch", 1
        raw = dist.read_text("direct_url.json")
        if raw is None:
            return None, "install_origin_unavailable", 3
        try:
            origin = json.loads(raw)
        except (ValueError, TypeError):
            return None, "install_origin_invalid", 3
        if not isinstance(origin, dict):
            return None, "install_origin_invalid", 3
        root, reason, code = _toolkit_origin(origin)
        if root is None:
            return None, reason, code
        files = dist.files
        if files is None or not any(str(file) == "t2p.py" for file in files):
            return None, "install_facade_mismatch", 1
        facade = Path(dist.locate_file("t2p.py")).resolve(strict=True)
        if facade != Path(__file__).resolve(strict=True) or facade.parent not in sites:
            return None, "install_facade_mismatch", 1
        return root, "", 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, RecursionError):
        return None, "install_metadata_unavailable", 3


def _bind_installed(root: Path) -> None:
    """Select live owners without chdir or persistent interpreter/profile changes."""
    global ROOT  # pylint: disable=global-statement
    ROOT = root
    sys.dont_write_bytecode = True
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["PATH"] = str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")
    sys.path.insert(0, str(root / "scripts"))


def _install_binding(result: dict) -> bool:
    root, reason, code = _installed_root()
    if root is not None:
        _bind_installed(root)
        return True
    result["exit_code"] = code
    result["status"] = (
        ("CANNOT_ESTABLISH" if result["command"] == "doctor" else "cannot_establish") if code == 3 else "error"
    )
    result["limitations"] = [reason]
    return False


def _install_human(result: dict) -> None:
    _print_safe(
        f"T2P INSTALL {result['limitations'][0]} (exit {result['exit_code']}). No owner was entered.\n"
        "Regularly reinstall the SAME <selected-toolkit> into <selected-python>:\n"
        "  For that checkout's .venv, from <selected-toolkit>:"
        " uv sync --all-extras --no-editable --reinstall-package tableau-to-pbi-migration\n"
        "  Or <selected-python> -m pip install <selected-toolkit> (pip must already be available).\n"
        "  uv pip install --reinstall-package tableau-to-pbi-migration"
        " --python <selected-python> <selected-toolkit> is only a plain/spaced-path compatibility form"
        " (without --editable).\n"
        "Raw native uv-pip combined Unicode/#/% compatibility is deferred;"
        " canonical uv sync and pip retain both path-class qualification targets."
        " Do not switch checkouts/environments to pass.\n"
        "No checkout fallback, preflight, login or repair was attempted."
    )


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
        "run_location_reason": (
            "not_applicable_no_run" if command in {"doctor", "units"} else "not_allocated_path_withheld"
        ),
        "path_report": None if command in {"doctor", "assess", "units"} else path_report(command),
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
        "owner": ALL_STAGE_OWNERS[name],
        "native_exit_code": code,
        "state": state,
        "artifact": artifact,
    }


def _doctor_preflight(stdout: bytes, stderr: bytes, native: int | None) -> dict:
    """Accept one complete, closed owner document; raw streams never become diagnostics."""
    # pylint: disable=too-many-return-statements
    unavailable = {
        "availability": "unavailable",
        "reason": "invalid_detail",
        "critical_missing": None,
        "recommended_warnings": None,
        "blockers": [],
    }
    if native is None or native not in {0, 1}:
        return {**unavailable, "reason": "launch_unavailable" if native is None else "unexpected_native"}
    if stderr and stderr.strip():
        return {**unavailable, "reason": "noisy_detail"}
    if not stdout or not stdout.strip():
        return {**unavailable, "reason": "missing_detail"}
    try:
        detail = json.loads(stdout)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return unavailable
    if not isinstance(detail, dict) or set(detail) != {"critical_missing", "recommended_warnings", "blockers"}:
        return unavailable
    if any(
        not isinstance(detail[key], int) or isinstance(detail[key], bool) or detail[key] < 0
        for key in ("critical_missing", "recommended_warnings")
    ):
        return unavailable
    if (
        not isinstance(detail["blockers"], list)
        or detail["critical_missing"] != len(detail["blockers"])
        or bool(detail["critical_missing"]) != bool(native)
    ):
        return unavailable
    allowed = {
        "copilot_home": ({"configured_directory_missing"}, "verify_copilot_home"),
        "python_runtime": ({"runtime_unavailable_or_old"}, "install_python"),
        "python_lxml": ({"import_failed"}, "sync_python_dependencies"),
        "python_jsonschema": ({"import_failed"}, "sync_python_dependencies"),
        "node_runtime": ({"runtime_unavailable_or_old"}, "install_node"),
        "report_author": ({"command_missing"}, "install_report_author"),
        "report_author_version": ({"unreadable", "prerelease", "below-floor"}, "inspect_cli_release"),
        "desktop_bridge_version": ({"unreadable", "prerelease", "below-floor"}, "inspect_cli_release"),
        "engine_plugin": ({"plugin_missing"}, "install_engine_plugin"),
        "engine_source": ({"verdict_unavailable"}, "inspect_engine_source"),
        "authoring_plugin": (
            {
                "missing",
                "disabled",
                "cache-missing",
                "manifest-unreadable",
                "unreadable",
                "prerelease",
                "manifest-mismatch",
                "below-floor",
                "skill-missing",
            },
            "repair_authoring_plugin_between_sessions",
        ),
        "reusable_skill_plugins": ({"multiple_installs"}, "inspect_duplicate_skill_plugins"),
        "skill_bundles": ({"bundle_missing"}, "sync_installed_skills"),
        "published_skill_bundles": ({"content_stale"}, "sync_and_publish_skills"),
        "npx": ({"command_missing"}, "install_node"),
        "desktop_bridge": ({"command_missing"}, "install_desktop_bridge"),
        "desktop_install": ({"executable_missing"}, "install_desktop"),
        "desktop_pin": ({"configured_path_missing"}, "refresh_current_process_pin"),
        "dotnet": ({"command_missing"}, "install_dotnet"),
        "adomd_client": ({"assembly_missing"}, "restore_adomd"),
        "fabric_tenant": ({"tenant_mismatch"}, "scope_intended_tenant"),
    }
    for blocker in detail["blockers"]:
        if (
            not isinstance(blocker, dict)
            or set(blocker) != {"id", "source", "reason", "remedy"}
            or not isinstance(blocker["id"], str)
            or blocker["id"] not in allowed
            or not isinstance(blocker["reason"], str)
        ):
            return unavailable
        reasons, remedy = allowed[blocker["id"]]
        source = "process_environment" if blocker["id"] == "desktop_pin" else None
        if blocker["reason"] not in reasons or blocker["remedy"] != remedy or blocker["source"] != source:
            return unavailable
    return {"availability": "available", "reason": "available", **detail}


def doctor(args: argparse.Namespace, result: dict) -> int:
    """Capture plain diagnostics privately; emit only owner-generated presence records."""
    from tableau_env import diagnose_env  # pylint: disable=import-outside-toplevel

    result["authority"] = "preflight.ps1+tableau_env.py"
    _values, configuration = diagnose_env(args.env or ROOT / ".env", explicit=args.env is not None)
    result["checks"] = configuration["checks"]
    result["limitations"] = [configuration["reason"], "presence_only_not_authentication"]
    result["doctor_details"] = {
        "configuration_aliases": configuration["configuration_aliases"],
        "preflight": _doctor_preflight(b"", b"", None),
    }
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    print("Checking existing preflight; no upgrades or Tableau login.", file=sys.stderr, flush=True)
    try:
        child = subprocess.run(
            [
                "powershell",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "preflight.ps1"),
                "-DoctorDiagnostics",
            ],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        code = child.returncode
        result["doctor_details"]["preflight"] = _doctor_preflight(
            getattr(child, "stdout", b""), getattr(child, "stderr", b""), code
        )
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


def _doctor_configuration_lines(result: dict) -> list[str]:
    sources = {
        "environment": "process environment",
        "selected_env": "selected environment file",
        "default": "default",
        "absent": "absent",
    }
    lines = []
    file_actions = {
        "selected_env_missing": "selected environment file is missing; restore or explicitly select the intended file.",
        "selected_env_unreadable": "selected environment file is unreadable; privately check its access and encoding.",
        "selected_env_malformed": "selected environment file is malformed; privately correct its KEY=VALUE syntax.",
    }
    for reason in result["limitations"]:
        if reason in file_actions:
            lines.append("Configuration: " + file_actions[reason])
    aliases = {row["key"]: row for row in result.get("doctor_details", {}).get("configuration_aliases", [])}
    for row in result["checks"]:
        key, state, source = row["key"], row["state"], sources[row["source"]]
        if row["required"]:
            lines.append(f"{key}: {state}; effective source: {source}. Values withheld.")
        if row["required"] and state in {"missing", "blank"}:
            lines.append(f"Action: privately supply the intended {key} in the selected file or invoking process.")
        if key not in aliases:
            continue
        legacy = "TABLEAU_PAT_VALUE" if key == "TABLEAU_PAT_SECRET" else "TABLEAU_SERVER"
        for origin in aliases[key]["sources"]:
            if legacy not in origin["keys"]:
                continue
            location = sources[origin["source"]]
            if origin["conflict"]:
                lines.append(f"{key}: conflict in {location}; spellings: {key} (canonical), {legacy} (deprecated).")
                lines.append(f"Effective source: {source}; conflict to resolve: {location}.")
            else:
                lines.append(f"Alias warning in {location}: {legacy} is deprecated; use {key}.")
            noun = "credential" if key == "TABLEAU_PAT_SECRET" else "setting"
            lines.append(
                f"Action: privately confirm the intended {noun}; preserve it under {key} BEFORE removing {legacy}"
                f" in that source. If {legacy} holds the intended value, move it to {key} first."
            )
    site = next((row for row in result["checks"] if row["key"] == "TABLEAU_SITE"), None)
    if site:
        lines.append(
            "TABLEAU_SITE: valid default."
            if site["reason"] == "default_site"
            else f"TABLEAU_SITE: {site['state']}; source: {sources[site['source']]}; value withheld."
        )
        lines.append("Optional source credentials: presence only, not validated; absence needs no repair.")
    return lines


def _doctor_blocker_lines(blocker: dict, *, installed: bool = False) -> list[str]:
    # #811: preflight also emits these plugin tokens for a dual-host refusal, which a host choice clears.
    choose_host = (
        "if preflight says it is installed for both the Copilot CLI and VS Code, choose one host with"
        " T2P_PLUGIN_HOST=cli or T2P_PLUGIN_HOST=vscode; otherwise "
    )
    labels = {
        "copilot_home": "configured Copilot home directory was not found",
        "python_runtime": "Python is unavailable, unreadable or below 3.11",
        "python_lxml": "Python cannot import lxml",
        "python_jsonschema": "Python cannot import jsonschema",
        "node_runtime": "Node.js is unavailable, unreadable or below 20",
        "report_author": "powerbi-report-author is not on PATH",
        "report_author_version": "powerbi-report-author release does not satisfy its existing floor check",
        "desktop_bridge_version": "powerbi-desktop release does not satisfy its existing floor check",
        "engine_plugin": "canonical conversion-engine plugin is missing or needs a host choice",
        "engine_source": "engine single-source requirement could not be satisfied",
        "authoring_plugin": "powerbi-authoring plugin does not satisfy its existing joint check",
        "reusable_skill_plugins": "multiple reusable-skill plugin installations were found",
        "skill_bundles": "installed reusable-skill bundles are incomplete",
        "published_skill_bundles": "installed reusable-skill content differs from the toolkit",
        "npx": "npx is not on PATH",
        "desktop_bridge": "powerbi-desktop is not on PATH",
        "desktop_install": "Power BI Desktop executable was not found",
        "desktop_pin": "PBI_DESKTOP_PATH is configured but its target was not found",
        "dotnet": ".NET SDK is not on PATH",
        "adomd_client": "ADOMD.NET client assembly is missing from the active NuGet cache",
        "fabric_tenant": "Fabric token does not match the explicitly intended tenant",
    }
    actions = {
        "verify_copilot_home": "privately verify COPILOT_HOME and create/correct the intended directory manually.",
        "install_python": "manually install/select Python 3.11+ on this process's PATH, then rerun.",
        "sync_python_dependencies": "activate the intended Python environment and run uv sync manually.",
        "install_node": "manually install/select Node.js 20+ including npx on this process's PATH.",
        "install_report_author": "manually install @microsoft/powerbi-report-authoring-cli per setup guidance.",
        "inspect_cli_release": "privately inspect that CLI; select a plain release meeting the existing preflight floor"
        " at session start only, using docs/operator-runbook.md.",
        "install_engine_plugin": choose_host + "install the canonical tableau-fabric-skills plugin between sessions"
        " per setup guidance.",
        "inspect_engine_source": "run python -B scripts\\engine_source.py --json privately"
        " and restore a usable resolver/Python invocation.",
        "repair_authoring_plugin_between_sessions": choose_host + "close Copilot sessions; manually inspect/repair only"
        " powerbi-authoring@fabric-collection per docs/operator-runbook.md, then verify enabled/version/content.",
        "inspect_duplicate_skill_plugins": choose_host + "privately inspect duplicate skill-plugin installations;"
        " retain the intended copy and remove a duplicate manually between sessions.",
        "sync_installed_skills": "manually refresh installed bundles with scripts\\sync_installed_skills.py;"
        " install/update a new bundle's plugin only between sessions.",
        "sync_and_publish_skills": "manually synchronize installed skills and publish the intended toolkit content"
        " using setup guidance before trusting runtime results.",
        "install_desktop_bridge": "manually install @microsoft/powerbi-desktop-bridge-cli per setup guidance.",
        "install_desktop": "manually install Power BI Desktop, then verify and pin the intended executable.",
        "refresh_current_process_pin": "refresh the inherited PBI_DESKTOP_PATH in the process invoking doctor/Power BI."
        " A saved User setting does not update an already-running parent/session. If that saved pin is independently"
        " verified, reload it and rerun doctor in the SAME invocation; otherwise verify the installed executable and"
        " repin manually per setup guidance. Do not merely persist it again with setx or remove the pin to pass.",
        "install_dotnet": "manually install the .NET SDK per setup guidance.",
        "restore_adomd": "manually restore the separate ADOMD.NET client package into the active NuGet cache"
        " with a supported target framework; follow docs/operator-runbook.md.",
        "scope_intended_tenant": "privately verify intended identity; scope preflight with -Subscription for"
        " a subscription in that tenant. Do not rewrite the shared Azure CLI profile just to pass.",
    }
    if installed:
        actions["sync_python_dependencies"] = (
            "regularly reinstall the SAME <selected-toolkit> into <selected-python> per its README."
            " For that checkout's .venv, run uv sync --all-extras --no-editable"
            " --reinstall-package tableau-to-pbi-migration from <selected-toolkit>;"
            " otherwise use <selected-python> -m pip install <selected-toolkit>"
            " (pip must already be available), or uv pip install --reinstall-package tableau-to-pbi-migration"
            " --python <selected-python> <selected-toolkit> only for plain/spaced paths, without --editable."
            " Raw native uv-pip combined Unicode/#/% compatibility is deferred;"
            " canonical uv sync and pip retain both path-class qualification targets."
            " Do not switch checkouts/environments to pass."
        )
        actions = {
            key: (
                "In <selected-toolkit>, using <selected-python> for Python commands: " + action
                if "scripts\\" in action or "docs/" in action
                else action
            )
            for key, action in actions.items()
        }
    lines = [f"Preflight blocker: {labels[blocker['id']]}; reason: {blocker['reason']}."]
    if blocker["source"] == "process_environment":
        lines.append("Effective source: inherited process environment. This is not a PAT diagnosis.")
    lines.append("Action: " + actions[blocker["remedy"]])
    return lines


def _doctor_human(result: dict, args: argparse.Namespace | None, *, installed: bool = False) -> None:
    """Doctor alone has a human view; start/status keep their existing renderers."""
    lines = [f"DOCTOR {result['status']} (exit {result['exit_code']})"]
    lines.extend(_doctor_configuration_lines(result))
    stage = next((row for row in result["stages"] if row["stage"] == "preflight"), None)
    detail = result.get("doctor_details", {}).get("preflight", {})
    native = stage["native_exit_code"] if stage else None
    lines.append(f"Preflight owner: preflight.ps1; native exit: {native if native is not None else 'unavailable'}.")
    if detail.get("availability") == "available":
        lines.append(f"Recommended warnings: {detail['recommended_warnings']} (non-blocking).")
        for blocker in detail["blockers"]:
            lines.extend(_doctor_blocker_lines(blocker, installed=installed))
        lines.append("Preflight: passed." if native == 0 else "Preflight: blocked.")
    else:
        lines.append("Recommended-warning count: unavailable.")
        if native == 1:
            lines.append("Preflight blocked, but its safe cause could not be established.")
        elif native == 0:
            lines.append("Preflight passed; structured detail unavailable, so not every diagnostic was explained.")
        elif native is not None:
            lines.append(f"Unexpected preflight native exit {native}.")
        else:
            lines.append("Preflight did not complete; toolchain readiness is unknown.")
        if result["exit_code"] in {2, 130} or native is None:
            lines.append(
                {
                    2: "Action: use "
                    + ("<selected-t2p>" if installed else "python -B scripts\\t2p.py")
                    + " doctor --help for the supported syntax.",
                    130: "Action: rerun the same doctor after the interruption.",
                }.get(
                    result["exit_code"],
                    "Action: restore a usable PowerShell/toolkit invocation, then rerun the same doctor.",
                )
            )
        else:
            if installed:
                lines.append(
                    "Before manual preflight, activate the SAME environment that supplies <selected-python>"
                    " in this private terminal and verify PATH python is <selected-python>."
                    " Invoking <selected-t2p> does not activate the parent shell."
                )
            lines.append(
                "Action: run plain preflight directly in a private terminal without -Update, then rerun doctor."
            )
            lines.append(
                "  powershell -ExecutionPolicy Bypass -File "
                + ("<selected-toolkit>\\" if installed else "")
                + "scripts\\preflight.ps1"
            )
        lines.append("Do not change Tableau credentials on this evidence.")
    if result["status"] == "READY_TO_ATTEMPT":
        lines.append(
            "Next, only when approved: "
            + ("<selected-t2p>" if installed else "python -B scripts\\t2p.py")
            + " start --project <exact-name-or-LUID>"
        )
        lines.append("Keep the same --env selection if supplied.")
    rerun = ("<selected-t2p>" if installed else "python -B scripts\\t2p.py") + " doctor"
    if args is not None and args.env is not None:
        rerun += " --env <same-selected-env>"
    lines.extend(
        [
            "Rerun doctor: " + rerun,
            "No run, login or repair command was started.",
            "Presence/preflight only; not authentication, reachability, path safety, START_READY, fidelity,"
            " COMPLETE or deployment approval.",
        ]
    )
    _print_safe("\n".join(lines))


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


def start(args: argparse.Namespace, result: dict) -> int:  # pylint: disable=too-many-statements
    """One delegated front door; observer records outcomes, never parses child prose."""
    import start_migration  # pylint: disable=import-outside-toplevel

    result["authority"] = "start_migration.py"
    assessing = args.command == "assess"
    stage_names = (
        ("preflight", "survey", "assessment", "admin_insights", "report") if assessing else tuple(STAGE_OWNERS)
    )
    result["stages"] = [_stage(name, None, "not_launched") for name in stage_names]
    delegated = ["--assess-only"] if assessing else ["--project", args.project]
    for flag, value in (
        ("--env", args.env),
        ("--runs-parent", args.runs_parent),
        ("--storage-decision", None if assessing else getattr(args, "storage_decision", None)),
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
            result["stages"][stage_names.index(name)] = _stage(
                name,
                record["native_exit_code"],
                record["state"],
                record["artifact"],
            )
            if record["artifact"] is not None:
                reference = {
                    "owner": ALL_STAGE_OWNERS[name],
                    "locator": record["artifact"],
                    "check": "owner_validated" if record["state"] == "ok" else "not_established",
                }
                result["evidence"].append(reference)
                result["artifacts"].append(dict(reference))
            if (
                name == "bundle"
                and result["path_report"] is not None
                and result["path_report"]["projection_verdict"] is None
            ):
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
    if assessing:
        result["path_report"] = None
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
    result["limitations"].append(
        "assessment_only_not_migration_readiness" if assessing else "capture_only_not_completion"
    )
    return code


def status(args: argparse.Namespace, result: dict) -> int:
    """Project the owner's inventory, path budget and next command; never call path scanners or git."""
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
    # Arbitrary manifest strings and owner prose are deliberately not copied. Unit names, package
    # locators and the owner's next command are local operator output, like `t2p units`.
    result["observations"] = {
        "run_recorded_status": observed.get("run_recorded_status"),
        "inventory_scope": observed.get("inventory_scope"),
        "scope_denominator": observed.get("scope_denominator"),
        "canonical_subdirs": observed.get("canonical_subdirs"),
        "child_evidence": observed.get("child_evidence"),
        "findings": [
            {"code": row["code"], "reason": row["reason"], "unassessable": row["unassessable"]}
            for row in observed["findings"]
        ],
        "units": [
            {
                "ordinal": index,
                "id": row.get("id"),
                "name": row.get("name"),
                "stage": row.get("stage"),
                "working_package": row.get("working_package"),
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
    action = observed.get("next_action")
    result["next"] = (
        {key: action.get(key) for key in ("kind", "command", "headline", "owner", "gate", "affected_units")}
        if isinstance(action, dict) and "gate" in action
        else None
    )
    budget = observed.get("path_budget")
    if isinstance(budget, dict) and result["path_report"] is not None:
        report = result["path_report"]
        for kind in ("file", "directory"):
            measured = budget[kind]
            report[f"{kind}_ceiling_utf16"] = measured["ceiling"]
            for field, key in (
                ("longest_{}_utf16", "longest"),
                ("remaining_{}_utf16", "remaining"),
                ("longest_{}_locator", "path"),
            ):
                report[field.format(kind)] = {
                    "value": measured[key],
                    "reason": None if measured[key] is not None else "no_paths",
                }
        report["unknown_paths"] = budget["unknown"]
        report["runs_parent"] = budget["runs_parent"]
        report["root_kind"] = budget["root_kind"]
        rerun = budget.get("rerun")
        report["rerun"] = (
            {
                "argv": None,
                "command": rerun["command"],
                "runs_parent": rerun["runs_parent"],
                "authoritative": rerun["authoritative"],
                "reason": rerun.get("reason"),
            }
            if rerun
            else {"argv": None, "command": None, "authoritative": False, "reason": "within_ceilings"}
        )
    if observed.get("intent") == "assessment":
        result["observations"].update(
            {
                "intent": observed["intent"],
                "not_requested": observed["not_requested"],
                "assessment": observed["assessment"],
            }
        )
    result["limitations"] = ["last_observed_not_liveness", "customer_labels_local_only"]
    if observed.get("intent") == "assessment":
        result["limitations"].append("assessment_only_not_migration_readiness")
    result["proof_boundary"]["established"] = ["read_only_inventory"]
    return code


def units(args: argparse.Namespace, result: dict) -> int:
    """Project the read-only unit lookup; never allocate, rename, refresh or touch credentials."""
    import unit_ids  # pylint: disable=import-outside-toplevel
    from work_dirs import runs_root  # pylint: disable=import-outside-toplevel

    if args.units_action == "find" and args.name is None and args.luid is None:
        raise ArgumentError("units_find_needs_name_or_luid")
    root = runs_root(args.runs_parent.expanduser().resolve() if args.runs_parent is not None else None)
    result["authority"] = "unit_ids.py"
    if args.units_action == "show":
        lookup = unit_ids.show_unit(root, args.unit_id)
    elif args.units_action == "find":
        lookup = unit_ids.find_units(root, name=args.name, luid=args.luid)
    else:
        lookup = unit_ids.list_units(root)
    code = unit_ids.lookup_exit_code(lookup)
    result["delegated_exit_code"] = code
    result["status"] = {0: "ok", 1: "findings"}.get(code, "cannot_establish")
    result["unit_lookup"] = lookup
    result["limitations"] = ["read_only_lookup", "id_is_lookup_key_not_identity_proof", "customer_labels_local_only"]
    if any(row["state"] == "legacy" for row in lookup["runs"]):
        result["limitations"].append("legacy_runs_not_searched")
    result["proof_boundary"]["established"] = ["read_only_unit_lookup"]
    return code


def _units_human(result: dict) -> None:
    """One line per candidate; customer labels are local operator output, ASCII-escaped."""
    lookup = result.get("unit_lookup")
    if lookup is None:
        _print_safe(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
        return
    lines = [
        f"UNITS {lookup['action']} {lookup['result']}"
        + (f" ({lookup['reason']})" if lookup["reason"] else "")
        + f" (exit {result['exit_code']}) - read-only lookup; an ID is not identity proof",
        f"Runs root: {lookup['runs_root']}",
    ]
    for row in lookup["candidates"]:
        source = row["source"] or {}
        identity = [f"project={source.get('project_name') or 'unknown'}"] + [
            f"{field}={source[field]}" for field in ("luid", "project_luid", "site_luid") if source.get(field)
        ]
        artifacts = [
            f"{role}={view['relative_path']}" for role, view in (row["artifacts"] or {}).items() if view is not None
        ]
        lines.append(
            f"  {row['id']} {row['state']}"
            + (f" ({row['reason']})" if row["reason"] else "")
            + f"; run={row['run'] or 'unknown'}; kind={source.get('kind', 'unknown')}; "
            f"name={source.get('name', 'unknown')}"
        )
        if source:
            lines.append("    source: " + "; ".join(identity))
            lines.append("    artifacts: " + ("; ".join(artifacts) or "not emitted"))
    for row in lookup["runs"]:
        if row["state"] != "indexed":
            lines.append(f"  run {row['run']}: {row['state']} ({row['reason']})")
    _print_safe("\n".join(lines))


def _status_path_lines(report: dict, scalar: Callable[[Any], str]) -> list[str]:
    """Render the owner's path budget, or the static ceilings when it was not measured."""
    if "root_kind" not in report:
        return [
            "Paths: NOT CHECKED; current longest/remaining budgets not recorded; "
            "ceilings file=259, directory=247 UTF-16 code units."
        ]
    paths = [
        f"Paths: runs-parent={scalar(report.get('runs_parent'))} root={scalar(report.get('root_kind'))}; "
        f"unmeasured={scalar(report.get('unknown_paths'))}"
    ]
    for kind in ("file", "directory"):
        paths.append(
            f"  longest {kind}={scalar(report[f'longest_{kind}_utf16']['value'])} of "
            f"{scalar(report[f'{kind}_ceiling_utf16'])}; "
            f"remaining={scalar(report[f'remaining_{kind}_utf16']['value'])}; "
            f"path={scalar(report[f'longest_{kind}_locator']['value'])}"
        )
    rerun = report.get("rerun") or {}
    if rerun.get("command"):
        paths.append(
            "  Rerun: "
            + scalar(rerun["command"])
            + ("" if rerun.get("authoritative") else " (env and storage decision not recorded; check both)")
        )
    return paths


def _status_human(result: dict) -> None:
    """Explain only the final safe projection, without acquiring or certifying evidence."""

    def scalar(value: Any) -> str:
        if value is None:
            return "unavailable"
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=True)
        return "".join(char if " " <= char <= "~" else json.dumps(char, ensure_ascii=True)[1:-1] for char in text)

    def pairs(values: dict | None) -> str:
        if values is None:
            return "unavailable (not assessed)"
        return "; ".join(f"{scalar(key)}={scalar(value)}" for key, value in values.items()) or "{}"

    def observation_lines(observed: dict) -> list[str]:
        rows = []
        if observed.get("run_recorded_status") is not None:
            rows.append(
                "Recorded allocation status: "
                + scalar(observed["run_recorded_status"])
                + " (metadata only; not process liveness)"
            )
        if observed.get("inventory_scope") is not None:
            rows.append("Inventory scope: " + scalar(observed["inventory_scope"]))
        if observed.get("units") or observed.get("inventory_scope") == "established":
            rows.append("Units (ID or -, kind, name, stage, working package):")
        for unit in observed.get("units") or []:
            rows.append(
                f"  {scalar(unit.get('id') or '-')} {scalar(unit.get('kind'))} "
                f"{scalar(json.dumps(unit.get('name'), ensure_ascii=True))}; stage={scalar(unit.get('stage'))}; "
                f"working-package={scalar(unit.get('working_package'))}; scope={scalar(unit.get('scope'))}; "
                f"working-copy={scalar(unit.get('generated_working_copy'))}; handover={scalar(unit.get('handover'))}; "
                f"packages={scalar(unit.get('package_count'))}; "
                f"certification={scalar(unit.get('current_certification'))}"
            )
            for index, occurrence in enumerate(unit.get("occurrences", [])):
                rows.append(
                    f"    occurrence[{index}] kind={scalar(occurrence.get('kind'))} "
                    f"status={scalar(occurrence.get('status'))}"
                )
        if observed.get("units") == [] and observed.get("inventory_scope") == "established":
            rows.append("  none projected")
        rows.append("Findings (run-level; not associated with units):")
        for index, finding in enumerate(observed.get("findings") or []):
            rows.append(
                f"  [{index}] code={scalar(finding.get('code'))} reason={scalar(finding.get('reason'))} "
                f"unassessable={scalar(finding.get('unassessable'))} (run-level)"
            )
        if not observed.get("findings"):
            rows.append("  none projected" if observed.get("findings") == [] else "  unavailable (not assessed)")
        rows.append(
            "Unscoped packages: "
            + scalar(observed.get("unscoped_package_count"))
            + (" (projected count; inventory unavailable)" if observed.get("inventory_scope") is None else "")
        )
        for title, key in (
            ("Observed run folders", "canonical_subdirs"),
            ("Observed child evidence", "child_evidence"),
        ):
            if observed.get(key) is not None:
                rows.append(title + ": " + pairs(observed[key]))
        for index, phase in enumerate(observed.get("recorded_phases") or []):
            rows.append(
                f"Last observed phase[{index}]: source={scalar(phase.get('source'))}; "
                f"phase={scalar(phase.get('phase'))}; failed={scalar(phase.get('failed'))}; "
                f"last_observed: {pairs(phase.get('last_observed'))} - historical only, not a current check."
            )
        return rows

    observed = result.get("observations")
    now = "diagnostic observations unavailable (not assessed)."
    need = "inspect unavailable diagnostics; cause and required operator/lifecycle decision unknown."
    if observed is not None:
        assessing = observed.get("intent") == "assessment"
        now = (
            (
                "assessment evidence observed; no migration readiness or handoff was checked."
                if assessing
                else f"inventory scope={scalar(observed.get('inventory_scope'))}; "
                f"{len(observed.get('units', []))} observed units (not the commissioned denominator)."
            )
            if result.get("run_location") is not None
            else "selected run identity was not established; no accepted run or child location is shown."
        )
        inspection = bool(observed.get("findings")) or any(
            state is None or state in {"missing", "unestablished"} or str(state).startswith("unassessable")
            for state in (
                observed.get("inventory_scope"),
                *(observed.get("canonical_subdirs") or {}).values(),
                *(observed.get("child_evidence") or {}).values(),
                *(
                    unit.get(key)
                    for unit in observed.get("units", [])
                    for key in ("generated_working_copy", "handover")
                ),
            )
        )
        need = (
            (
                "inspect unassessable assessment evidence before deciding next steps."
                if assessing
                else (
                    "inspect missing/unassessable/finding observations; cause and required operator/lifecycle "
                    "decision unknown."
                )
            )
            if inspection
            else (
                "review the HTML assessment and choose scope; this does not establish migration readiness."
                if assessing
                else "this diagnostic establishes no required operator/lifecycle decision."
            )
        )
    lines = [
        f"STATUS {scalar(result.get('status'))} (exit {scalar(result.get('exit_code'))}) - observations only",
        "Goal: inspect the explicitly selected run.",
        "Now: " + now,
        "Observed handoff: NOT CHECKED - openability, START_READY and COMPLETE were not evaluated.",
        "Need: " + need,
        f"Existing owner: {scalar(result.get('authority')) if result.get('authority') is not None else 'not reached'}; "
        f"delegated diagnostic exit={scalar(result.get('delegated_exit_code'))}; "
        f"facade exit={scalar(result.get('exit_code'))}.",
    ]
    if result.get("run_location") is not None:
        lines.append(
            "Run: "
            + scalar(result["run_location"]["selected_run"])
            + " (supplied local path accepted by the owner; local-only, not publication consent)"
        )
    if observed is not None:
        lines.extend(observation_lines(observed))
        if observed.get("intent") == "assessment":
            assessment = observed.get("assessment") or {}
            lines.extend(
                [
                    "Intent: assessment",
                    "Not requested: " + pairs(observed.get("not_requested")),
                    f"Assessment: {scalar(assessment.get('assessment'))} "
                    f"({scalar(assessment.get('assessment_reason'))})",
                    f"Admin Insights: {scalar(assessment.get('admin_insights'))} "
                    f"({scalar(assessment.get('admin_insights_reason'))})",
                    f"Report: {scalar(assessment.get('report'))} ({scalar(assessment.get('report_reason'))})",
                ]
            )
    paths = _status_path_lines(result.get("path_report") or {}, scalar)
    action = result.get("next")
    steps = (
        [
            "Next: " + scalar(action.get("command") or action.get("headline")),
            f"Blocked by: {scalar(action.get('gate'))} (owner {scalar(action.get('owner'))}; "
            f"kind {scalar(action.get('kind'))})",
        ]
        if action
        else []
    )
    if observed is not None and observed.get("intent") == "assessment":
        lines.extend(steps)
    else:
        lines.extend(
            [
                *paths,
                *steps,
                "Unavailable here: original intent/revision; openability; recorded parent/root/repo/git/commit "
                "state; subst.",
            ]
        )
    _print_safe("\n".join(lines))


def _print_safe(text: str) -> None:
    """ASCII escapes keep human diagnostics printable even under cp1252."""
    print(text.encode("ascii", errors="backslashreplace").decode("ascii"), flush=True)


def _delegate(args: argparse.Namespace, result: dict, *, json_mode: bool) -> int:
    """Keep the same three owners and live descriptor transport, without a command registry."""
    if args.command == "doctor":
        code = doctor(args, result)
    elif args.command in {"start", "assess"}:
        # The path marker is local progress, not part of the stdout JSON stream.
        if json_mode:
            with _native_to_stderr():
                code = start(args, result)
        else:
            code = start(args, result)
    elif args.command == "units":
        code = units(args, result)
    else:
        code = status(args, result)
    return code


# pylint: disable=too-many-branches
def main(argv: list[str] | None = None, *, installed: bool = False) -> int:
    """Render one versioned result without reflecting exception text or invalid argv."""
    argv = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in argv
    command = argv[0] if argv and argv[0] in {"doctor", "start", "assess", "status", "units"} else None
    result = envelope(command)
    args = None
    install_failure = False
    try:
        parser = build_parser("t2p") if installed else build_parser()
        args = parser.parse_args(argv)
        if args.command == "start" and not args.project.strip():
            raise ArgumentError("nonempty_project_required")
        install_failure = installed and not _install_binding(result)
        code = result["exit_code"] if install_failure else _delegate(args, result, json_mode=json_mode)
    except ArgumentError:
        code = 2
        result["limitations"] = ["invalid_arguments_use_help"]
    except KeyboardInterrupt:
        code = 130
        result["status"] = "interrupted"
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, ImportError) and not installed:
            raise
        code = 3 if command == "doctor" else 1
        result["status"] = "CANNOT_ESTABLISH" if command == "doctor" else "error"
        result["limitations"].append("owner_result_unavailable")
    result["exit_code"] = code
    if json_mode:
        print(json.dumps(result, ensure_ascii=True, sort_keys=True), flush=True)
    elif install_failure:
        _install_human(result)
    elif command == "status":
        _status_human(result)
    elif command == "units":
        _units_human(result)
    elif command == "doctor":
        if installed:
            _doctor_human(result, args, installed=True)
        else:
            _doctor_human(result, args)
    else:
        _print_safe(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=2))
    if (
        args is not None
        and args.command in {"start", "assess"}
        and (not installed or result["authority"] == "start_migration.py")
    ):
        line = _run_line(result, args)
        if json_mode:
            print(line, file=sys.stderr, flush=True)
        else:
            _print_safe(line)
    return code


def console_main() -> int:
    """Standard installed console entry; direct main() remains the expert compatibility path."""
    return main(installed=True)


if __name__ == "__main__":
    raise SystemExit(console_main() if __spec__ is not None else main())
