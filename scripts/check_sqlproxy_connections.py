"""
purpose: gate shipped semantic models that still point at Tableau's `sqlproxy` published-datasource
         protocol instead of a real upstream connection.
usage:   python scripts/check_sqlproxy_connections.py <bundle-or-model-dir> [...]
                                                    [--json <file>] [--quiet] [--warn-only]

Why this exists
---------------
`sqlproxy` is Tableau's own protocol for published datasources. Power BI cannot speak it. When the
engine emits `Server_sqlproxy*` / `Database_sqlproxy*` parameters into a `.SemanticModel`, the model
is still pointed at Tableau (or Tableau's local proxy) rather than the source system that must be
migrated. The artifact can validate cleanly and still be unusable for those tables.

Detection deliberately keys on the parameter names, not on the server value. The field report used
`localhost`, but the committed estate bundles use Tableau Cloud hosts such as `10ax.online.tableau.com`.
The defect is the protocol, not the address.

A secondary warning reads native engine `report.json` telemetry: a built workbook with non-empty
`binding_signal.secondary_datasources` is the at-risk shape that can lead to sqlproxy output. That
signal is useful triage, but it is not itself a shipped broken connection, so it never changes the
exit code. The blocking verdict comes only from `expressions.tmdl` on disk.

Exit codes
----------
| 0 | scan ran and no sqlproxy-derived model connections were found. Warnings may exist.
| 1 | at least one shipped model contains a Server_sqlproxy* / Database_sqlproxy* pair.
| 2 | usage error (argparse) - a missing path never produces a verdict.
| 3 | SKIPPED: no semantic model was found, so nothing was measured.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bundle_corpus import shipping_models

REPORT_NAME = "sqlproxy-connection-check.json"

STATUS_OK = "OK"
STATUS_SQLPROXY = "SQLPROXY"
STATUS_SKIPPED = "SKIPPED"

EXIT_OK = 0
EXIT_SQLPROXY = 1
EXIT_USAGE = 2
EXIT_SKIPPED = 3

_NAME = r"""'(?:[^']|'')*'|#"(?:[^"]|"")*"|[^\s=]+"""
_EXPRESSION_RE = re.compile(rf"^\s*expression\s+(?P<name>{_NAME})\s*=\s*(?P<value>.*)$")
_SERVER_RE = re.compile(r"^Server_sqlproxy(?P<suffix>\d*)$", re.IGNORECASE)
_DATABASE_RE = re.compile(r"^Database_sqlproxy(?P<suffix>\d*)$", re.IGNORECASE)
_STRING_RE = re.compile(r'^"(?P<value>(?:[^"]|"")*)"')
_META_RE = re.compile(r"\s+meta\s*\[.*$", re.IGNORECASE)


@dataclass(frozen=True)
class Parameter:
    """One sqlproxy parameter from `expressions.tmdl`."""

    name: str
    suffix: str
    value: str
    line: int


@dataclass(frozen=True)
class Pair:
    """The actionable Server_sqlproxy* / Database_sqlproxy* pair."""

    suffix: str
    server_name: str | None
    server: str | None
    server_line: int | None
    database_name: str | None
    database: str | None
    database_line: int | None

    @property
    def complete(self) -> bool:
        """Whether both halves of the emitted connection are present."""
        return self.server_name is not None and self.database_name is not None


@dataclass(frozen=True)
class RiskSignal:
    """A built workbook whose engine handover names secondary published datasource dependencies."""

    workbook: str
    path: str
    pbip_status: str
    secondary_datasources: list[str]


def _unquote(name: str) -> str:
    """Strip TMDL quoting from an identifier."""
    name = name.strip()
    if len(name) >= 2 and name.startswith("'") and name.endswith("'"):
        return name[1:-1].replace("''", "'")
    if len(name) >= 3 and name.startswith('#"') and name.endswith('"'):
        return name[2:-1].replace('""', '"')
    return name


def _string_value(value: str) -> str:
    """Return the first TMDL string literal value, or a readable scalar fallback."""
    value = value.strip()
    match = _STRING_RE.match(value)
    if match:
        return match.group("value").replace('""', '"')
    return _META_RE.sub("", value).strip()


def parse_expressions(path: Path) -> list[Parameter]:
    """Read sqlproxy parameters from one `expressions.tmdl` file."""
    parameters: list[Parameter] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return parameters
    for index, line in enumerate(lines, start=1):
        match = _EXPRESSION_RE.match(line)
        if not match:
            continue
        name = _unquote(match.group("name"))
        server = _SERVER_RE.match(name)
        database = _DATABASE_RE.match(name)
        if not server and not database:
            continue
        parameters.append(
            Parameter(
                name=name,
                suffix=(server or database).group("suffix"),
                value=_string_value(match.group("value")),
                line=index,
            )
        )
    return parameters


def _pairs(parameters: list[Parameter]) -> list[Pair]:
    """Pair Server_sqlproxy* and Database_sqlproxy* by suffix."""
    servers = {parameter.suffix: parameter for parameter in parameters if _SERVER_RE.match(parameter.name)}
    databases = {parameter.suffix: parameter for parameter in parameters if _DATABASE_RE.match(parameter.name)}
    pairs = []
    for suffix in sorted(set(servers) | set(databases), key=lambda value: (value != "", int(value or 0))):
        server = servers.get(suffix)
        database = databases.get(suffix)
        pairs.append(
            Pair(
                suffix=suffix,
                server_name=server.name if server else None,
                server=server.value if server else None,
                server_line=server.line if server else None,
                database_name=database.name if database else None,
                database=database.value if database else None,
                database_line=database.line if database else None,
            )
        )
    return pairs


def _finding(pair: Pair, model_dir: Path, expressions_path: Path) -> dict[str, Any]:
    """Shape one pair for JSON output and the rendered queue."""
    try:
        tmdl = expressions_path.resolve().relative_to(model_dir.resolve().parent).as_posix()
    except ValueError:
        tmdl = expressions_path.as_posix()
    return {
        "suffix": pair.suffix,
        "server_parameter": pair.server_name,
        "server": pair.server,
        "server_line": pair.server_line,
        "database_parameter": pair.database_name,
        "database": pair.database,
        "database_line": pair.database_line,
        "complete": pair.complete,
        "tmdl": tmdl,
    }


def scan_model(model_dir: Path) -> dict[str, Any]:
    """Scan one semantic model for sqlproxy-derived expression parameters."""
    expressions_path = model_dir / "definition" / "expressions.tmdl"
    findings = [_finding(pair, model_dir, expressions_path) for pair in _pairs(parse_expressions(expressions_path))]
    return {
        "model": model_dir.name,
        "path": str(model_dir),
        "status": STATUS_SQLPROXY if findings else STATUS_OK,
        "connections": len(findings),
        "incomplete_connections": sum(1 for finding in findings if not finding["complete"]),
        "findings": findings,
    }


find_models = shipping_models


def scan(root: Path) -> dict[str, Any]:
    """Scan every shipping semantic model and engine risk signal under one path."""
    models = [scan_model(model_dir) for model_dir in find_models(root)]
    return merge(models, _risk_signals(root))


def merge(models: list[dict[str, Any]], risks: list[RiskSignal] | None = None) -> dict[str, Any]:
    """Fold per-model reports into one verdict."""
    risks = risks or []
    failing = [model for model in models if model["status"] == STATUS_SQLPROXY]
    if not models:
        status = STATUS_SKIPPED
    else:
        status = STATUS_SQLPROXY if failing else STATUS_OK
    return {
        "status": status,
        "models_scanned": len(models),
        "models_with_sqlproxy": len(failing),
        "connections": sum(model["connections"] for model in models),
        "incomplete_connections": sum(model["incomplete_connections"] for model in models),
        "models": models,
        "at_risk_workbooks": [risk.__dict__ for risk in risks],
        "warnings": len(risks),
    }


def _risk_signals(root: Path) -> list[RiskSignal]:
    """Read non-blocking secondary-datasource risk signals from native engine `report.json`."""
    report_paths = [root / "report.json"] if (root / "report.json").is_file() else sorted(root.rglob("report.json"))
    risks: list[RiskSignal] = []
    for report_path in report_paths:
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        for workbook in _workbooks(payload):
            signal = workbook.get("binding_signal") if isinstance(workbook.get("binding_signal"), dict) else {}
            secondary = signal.get("secondary_datasources") if isinstance(signal, dict) else None
            if workbook.get("pbip_status") != "built" or not isinstance(secondary, list) or not secondary:
                continue
            risks.append(
                RiskSignal(
                    workbook=str(workbook.get("name") or workbook.get("workbook") or "<unnamed>"),
                    path=str(report_path),
                    pbip_status="built",
                    secondary_datasources=[str(item) for item in secondary],
                )
            )
    return risks


def _workbooks(value: Any) -> list[dict[str, Any]]:
    """Return workbook-shaped dicts from an engine report payload."""
    if isinstance(value, dict):
        workbooks = value.get("workbooks")
        if isinstance(workbooks, list):
            return [item for item in workbooks if isinstance(item, dict)]
    return []


def render(report: dict[str, Any]) -> str:
    """Human-readable verdict, matching the sibling offline gates."""
    if report["status"] == STATUS_SKIPPED:
        return "SQLPROXY CONNECTION CHECK: SKIPPED - nothing measured (no semantic model found)"
    warning_tail = _warning_tail(report)
    if report["status"] == STATUS_OK:
        return (
            f"SQLPROXY CONNECTION CHECK: OK - no sqlproxy-derived connection parameters in "
            f"{report['models_scanned']} model(s).{warning_tail}"
        )
    lines = [
        f"SQLPROXY CONNECTION CHECK: SQLPROXY - {report['connections']} sqlproxy-derived "
        f"connection pair(s) in {report['models_with_sqlproxy']} of {report['models_scanned']} model(s).{warning_tail}"
    ]
    for model in report["models"]:
        if model["status"] != STATUS_SQLPROXY:
            continue
        lines.append(f"  {model['model']}")
        for finding in model["findings"]:
            lines.append(
                "    - "
                f"{finding['server_parameter']}={finding['server']!r} / "
                f"{finding['database_parameter']}={finding['database']!r} "
                f"({finding['tmdl']}:{finding['database_line'] or finding['server_line']})"
            )
    lines.append(
        "  Database_sqlproxy* names the Tableau published datasource that must be migrated or rebound; "
        "Power BI cannot query Tableau sqlproxy directly."
    )
    return "\n".join(lines)


def _warning_tail(report: dict[str, Any]) -> str:
    """Render at-risk report.json telemetry without changing the blocking verdict."""
    risks = report.get("at_risk_workbooks") or []
    if not risks:
        return ""
    names = ", ".join(f"{risk['workbook']} ({len(risk['secondary_datasources'])} secondary)" for risk in risks)
    return f"\n  WARN: {len(risks)} built workbook(s) declare secondary_datasources in binding_signal: {names}"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="bundle folder(s) or .SemanticModel folder(s)")
    parser.add_argument("--model", type=Path, help="explicit .SemanticModel folder")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--warn-only", action="store_true", help="always exit 0 after a successful scan")
    args = parser.parse_args(argv)

    targets = [*args.paths, *([args.model] if args.model else [])]
    if not targets:
        parser.error("give a bundle/model path, or --model")
    for path in targets:
        if not path.is_dir():
            parser.error(f"{path} is not a directory")

    scans = [scan(path) for path in targets]
    merged = merge(
        [model for report in scans for model in report["models"]],
        [RiskSignal(**risk) for report in scans for risk in report.get("at_risk_workbooks", [])],
    )

    if args.json:
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged))
    if args.warn_only:
        return EXIT_OK
    if merged["status"] == STATUS_SKIPPED:
        return EXIT_SKIPPED
    if merged["status"] == STATUS_SQLPROXY:
        return EXIT_SQLPROXY
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
