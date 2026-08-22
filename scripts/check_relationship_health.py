"""
purpose: gate sparse semantic models where date-bearing fact tables are stranded from Date.
usage:   python scripts/check_relationship_health.py <bundle-or-model-dir> [...] [--json <file>] [--quiet] [--warn-only]

Why this exists
---------------
`check_field_bindings.py` already catches a visual whose grouping columns span unrelated tables. Issue
#277 is the complementary model-owner signal: a semantic model can have a nearly-empty relationship
graph and a date-bearing core table disconnected from the Date table. That artifact can pass model
and PBIR validation, and it should be routed to the model owner instead of guessed at in the report.

This script deliberately reuses `check_field_bindings.parse_model()` and its relationship component
logic, including the existing `detached_ok` exemptions for field parameters and calculation groups.
It adds only the date-column census needed to decide whether a non-detached table is likely expected
to join the Date/Calendar table.

Exit codes
----------
| 0 | scan ran and no stranded date-bearing table was found. Relationship-count warnings may exist. |
| 1 | at least one model has a date-bearing table disconnected from every Date/Calendar table. |
| 2 | usage error (argparse) - a missing path never produces a verdict. |
| 3 | SKIPPED: no semantic model was found, so nothing was measured. |
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bundle_corpus import shipping_models

import check_field_bindings as cfb

REPORT_NAME = "relationship-health-check.json"

STATUS_OK = "OK"
STATUS_MISSING = "MISSING_RELATIONSHIP"
STATUS_SKIPPED = "SKIPPED"

EXIT_OK = 0
EXIT_MISSING = 1
EXIT_USAGE = 2
EXIT_SKIPPED = 3

MIN_FACT_COLUMNS = 3

_NAME = r"'(?:[^']|'')*'|[^\s=]+"
_TABLE_RE = re.compile(rf"^table\s+(?P<name>{_NAME})\s*$")
_COLUMN_RE = re.compile(rf"^(?P<indent>[\t ]+)column\s+(?P<name>{_NAME})(?:\s*=.*)?$")
_DATA_TYPE_RE = re.compile(r"^\s*dataType\s*:\s*(?P<value>\S+)")
_DATE_TABLE_RE = re.compile(r"(^|[_\s-])(date|calendar|dim[_\s-]*date)([_\s-]|$)", re.IGNORECASE)
_DATE_COLUMN_RE = re.compile(r"(date|datetime|opened|closed|created|updated|_at$)", re.IGNORECASE)


@dataclass
class ColumnInfo:
    """One TMDL column with enough metadata to classify date-bearing facts."""

    name: str
    data_type: str | None = None
    line: int | None = None

    @property
    def is_date_like(self) -> bool:
        """Whether this column is likely a date role needing a Date/Calendar relationship."""
        dtype = (self.data_type or "").casefold()
        return dtype in {"date", "datetime"} or bool(_DATE_COLUMN_RE.search(self.name))


@dataclass
class TableInfo:
    """Date-column census for one semantic-model table."""

    columns: dict[str, ColumnInfo] = field(default_factory=dict)

    @property
    def date_columns(self) -> list[ColumnInfo]:
        """Columns that look date-bearing by data type or name."""
        return [column for column in self.columns.values() if column.is_date_like]


def _unquote(name: str) -> str:
    """Strip TMDL single-quoting from an object name."""
    return cfb._unquote(name)  # pylint: disable=protected-access


def _parse_column_census(model_dir: Path) -> dict[str, TableInfo]:
    """Read top-level table columns and their `dataType` from TMDL files."""
    root = model_dir / "definition"
    if not root.is_dir():
        root = model_dir
    tables: dict[str, TableInfo] = {}
    for path in sorted(root.rglob("*.tmdl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        _parse_tmdl_columns(lines, tables)
    return tables


def _parse_tmdl_columns(lines: list[str], tables: dict[str, TableInfo]) -> None:
    """Fold the columns declared in one TMDL file into `tables`."""
    current_table: str | None = None
    current_column: ColumnInfo | None = None
    member_indent: int | None = None
    for index, line in enumerate(lines, start=1):
        table_match = _TABLE_RE.match(line)
        if table_match:
            current_table = _unquote(table_match.group("name"))
            tables.setdefault(current_table, TableInfo())
            current_column = None
            member_indent = None
            continue
        if current_table is None:
            continue
        column_match = _COLUMN_RE.match(line)
        if column_match:
            indent = len(column_match.group("indent").expandtabs(4))
            if member_indent is None or indent < member_indent:
                member_indent = indent
            if indent == member_indent:
                name = _unquote(column_match.group("name"))
                current_column = ColumnInfo(name=name, line=index)
                tables[current_table].columns[name] = current_column
                continue
        data_type = _DATA_TYPE_RE.match(line)
        if data_type and current_column is not None:
            current_column.data_type = data_type.group("value")


find_models = shipping_models


def _is_date_table(name: str, _info: TableInfo) -> bool:
    """Whether this table is the model's explicit Date/Calendar dimension.

    A column called `Date` alone is not enough: committed examples include disconnected helper tables
    such as `Date for Calendar`, and treating those as canonical dimensions turns a design choice into
    a false positive. The customer shape named an explicit `Date` table, so keep the gate narrow.
    """
    normalized = name.replace("_", " ").replace("-", " ").strip().casefold()
    return normalized in {"date", "calendar", "dim date", "date table"}


def _relationshipable_tables(model: cfb.ModelFields) -> list[str]:
    """Tables that should participate in ordinary relationship health checks."""
    return sorted(name for name in model.tables if name not in model.detached_ok)


def _sparse_relationship_graph(model: cfb.ModelFields) -> bool:
    """A low-noise model-wide sparsity signal.

    One relationship can be enough for a two-table model. It is suspicious when three or more
    relationshipable tables have fewer than `tables - 1` active joins, because that necessarily leaves
    at least one component disconnected.
    """
    tables = _relationshipable_tables(model)
    active_relationships = sum(1 for rel in model.relationships if rel.is_active)
    return len(tables) >= 3 and active_relationships < len(tables) - 1


def _stranded_date_tables(model: cfb.ModelFields, census: dict[str, TableInfo]) -> list[dict[str, Any]]:
    """Find non-detached date-bearing tables disconnected from all Date/Calendar dimensions."""
    relationshipable = set(_relationshipable_tables(model))
    date_tables = [name for name, info in census.items() if name in relationshipable and _is_date_table(name, info)]
    date_components = {model.components().get(name.casefold(), name.casefold()) for name in date_tables}
    findings: list[dict[str, Any]] = []
    if not date_tables or not date_components or not _sparse_relationship_graph(model):
        return findings
    for table in sorted(relationshipable):
        if table in date_tables:
            continue
        info = census.get(table, TableInfo())
        date_columns = info.date_columns
        if not date_columns or len(info.columns) < MIN_FACT_COLUMNS:
            continue
        component = model.components().get(table.casefold(), table.casefold())
        if component in date_components:
            continue
        findings.append(
            {
                "table": table,
                "date_columns": [column.name for column in date_columns],
                "date_column_lines": {column.name: column.line for column in date_columns},
                "component": component,
                "date_tables": date_tables,
            }
        )
    return findings


def _has_tmdl_documents(model_dir: Path) -> bool:
    """Whether this model has any TMDL document for the checker to inspect."""
    root = model_dir / "definition"
    if not root.is_dir():
        root = model_dir
    return any(path.is_file() for path in root.rglob("*.tmdl"))


def scan_model(model_dir: Path) -> dict[str, Any]:
    """Scan one `.SemanticModel` for sparse Date relationship gaps."""
    model = cfb.parse_model(model_dir)
    census = _parse_column_census(model_dir)
    active_relationships = sum(1 for rel in model.relationships if rel.is_active)
    relationshipable = _relationshipable_tables(model)
    findings = _stranded_date_tables(model, census)
    status = STATUS_MISSING if findings else (STATUS_OK if _has_tmdl_documents(model_dir) else STATUS_SKIPPED)
    date_tables = [name for name, info in census.items() if name in relationshipable and _is_date_table(name, info)]
    return {
        "model": model_dir.name,
        "path": str(model_dir),
        "status": status,
        "tables": len(model.tables),
        "relationshipable_tables": len(relationshipable),
        "relationships": len(model.relationships),
        "active_relationships": active_relationships,
        "relationship_components": len(
            {model.components().get(name.casefold(), name.casefold()) for name in relationshipable}
        ),
        "date_tables": date_tables,
        "sparse_relationship_graph": _sparse_relationship_graph(model),
        "detached_ok": model.detached_ok,
        "findings": findings,
    }


def scan(root: Path) -> dict[str, Any]:
    """Scan every shipping semantic model under one path."""
    return merge([scan_model(model_dir) for model_dir in find_models(root)])


def merge(models: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-model reports into one verdict."""
    failing = [model for model in models if model["status"] == STATUS_MISSING]
    skipped = [model for model in models if model["status"] == STATUS_SKIPPED]
    if not models:
        status = STATUS_SKIPPED
    else:
        status = STATUS_MISSING if failing else (STATUS_SKIPPED if skipped else STATUS_OK)
    return {
        "status": status,
        "models_scanned": len(models),
        "models_with_missing_relationships": len(failing),
        "findings": sum(len(model["findings"]) for model in models),
        "relationships": sum(model["relationships"] for model in models),
        "active_relationships": sum(model["active_relationships"] for model in models),
        "models": models,
    }


def render(report: dict[str, Any]) -> str:
    """Human-readable verdict, matching sibling offline gates."""
    if report["status"] == STATUS_SKIPPED:
        return "RELATIONSHIP HEALTH CHECK: SKIPPED - nothing measured (no semantic model or TMDL files found)"
    if report["status"] == STATUS_OK:
        sparse = sum(1 for model in report["models"] if model["sparse_relationship_graph"])
        tail = f" {sparse} sparse graph(s) reported for review." if sparse else ""
        return (
            f"RELATIONSHIP HEALTH CHECK: OK - no stranded date-bearing tables in "
            f"{report['models_scanned']} model(s); {report['active_relationships']} active relationship(s).{tail}"
        )
    lines = [
        f"RELATIONSHIP HEALTH CHECK: MISSING_RELATIONSHIP - {report['findings']} date-bearing table(s) "
        f"stranded from Date/Calendar in {report['models_with_missing_relationships']} of "
        f"{report['models_scanned']} model(s); {report['active_relationships']} active relationship(s)."
    ]
    for model in report["models"]:
        if model["status"] != STATUS_MISSING:
            continue
        lines.append(
            f"  {model['model']}: {model['active_relationships']} active relationship(s), "
            f"{model['relationship_components']} component(s), Date tables: {', '.join(model['date_tables'])}"
        )
        for finding in model["findings"]:
            columns = ", ".join(finding["date_columns"])
            lines.append(f"    - {finding['table']} has date column(s) [{columns}] but no path to Date/Calendar")
    lines.append(
        "  This is a model-owner decision: add the intended relationship, or document why the fact-like table "
        "is disconnected."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", type=Path, help="bundle folder(s) or .SemanticModel folder(s)")
    parser.add_argument("--json", type=Path, help="write the machine-readable verdict here")
    parser.add_argument("--quiet", action="store_true", help="suppress the rendered verdict")
    parser.add_argument("--warn-only", action="store_true", help="always exit 0 after a successful scan")
    args = parser.parse_args(argv)

    if not args.paths:
        parser.error("give a bundle/model path")
    for path in args.paths:
        if not path.is_dir():
            parser.error(f"{path} is not a directory")

    merged = merge([model for path in args.paths for model in scan(path)["models"]])
    if args.json:
        args.json.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(render(merged))
    if args.warn_only:
        return EXIT_OK
    if merged["status"] == STATUS_SKIPPED:
        return EXIT_SKIPPED
    if merged["status"] == STATUS_MISSING:
        return EXIT_MISSING
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
