"""
purpose: emit the CORRECT Power Query M for each data source in a migration-spec.json, deterministically,
         so the semantic-model builder never has to remember (or invent) a connector's navigation shape.

         Why this exists, concretely: a build agent asked to model a live Databricks source wrote

             Source = Databricks.Catalogs(host, httpPath),
             Catalog = Source{[Catalog="dbx_workspace"]}[Data],
             Schema  = Catalog{[Schema="tableau_migration"]}[Data],

         which is wrong on three of four lines - the navigation keys are `Name` + `Kind`, and the
         catalog level's Kind is the counter-intuitive "Database". The correct shape was already
         verified first-hand and written down in docs/data-source-credentials.md, but that doc is not
         reachable from the builder's persona (a custom-agent subagent receives ONLY its own persona
         file), so the knowledge could not fire. Prose in an unreachable file is not a control; a
         script is. This is that script.

         `--introspect` goes one step further and reads the REAL column names/types from the source
         (Databricks Statement Execution API via the `databricks` CLI), because the same run also
         invented column names (`shipment_date`, `height_cm`) that do not exist. Schema is knowable;
         it should never be guessed.

usage:   python scripts/emit_source_m.py --spec migrations/workbooks/<slug>/migration-spec.json
         python scripts/emit_source_m.py --spec <spec> --source "Shipment (Databricks)" --introspect
         python scripts/emit_source_m.py --spec <spec> --warehouse <id> --introspect

Prints one M expression per data source. Nothing is written to the model - the builder pastes it into
the partition, or diffs it against what it already wrote.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("emit_source_m")

# Databricks SQL type -> Power Query type. Anything unmapped is left untyped on purpose: guessing a
# type is how a silent precision/rounding bug gets in.
_DBX_TO_M = {
    "boolean": "type logical",
    "date": "type date",
    "timestamp": "type datetime",
    "timestamp_ntz": "type datetime",
    "string": "type text",
    "int": "Int64.Type",
    "integer": "Int64.Type",
    "bigint": "Int64.Type",
    "smallint": "Int64.Type",
    "tinyint": "Int64.Type",
    "long": "Int64.Type",
    "double": "type number",
    "float": "type number",
    "real": "type number",
}


def _m_type(sql_type: str) -> str | None:
    """Map a Databricks SQL type name to a Power Query type literal, or None when unmapped."""
    base = (sql_type or "").strip().lower().split("(")[0]
    if base.startswith("decimal"):
        return "type number"
    return _DBX_TO_M.get(base)


def describe_databricks_table(warehouse: str, qualified: str) -> list[tuple[str, str]]:
    """Return [(column, sql_type)] for a fully-qualified Databricks table, via the databricks CLI.

    Deliberately deterministic and read-only: `DESCRIBE TABLE` costs nothing and removes the entire
    schema-guessing surface. Returns [] (and warns) if the CLI is unavailable or the call fails, so
    the caller degrades to an untyped-but-correct navigation rather than failing outright.
    """
    body = {"warehouse_id": warehouse, "statement": f"DESCRIBE TABLE {qualified}", "wait_timeout": "50s"}
    tmp = Path(tempfile.mkdtemp(prefix="describe_")) / "body.json"
    tmp.write_text(json.dumps(body), encoding="utf-8")
    try:
        proc = subprocess.run(
            ["databricks", "api", "post", "/api/2.0/sql/statements", "--json", f"@{tmp}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            check=False,
        )
    except FileNotFoundError:
        log.warning("  (databricks CLI not on PATH - emitting untyped navigation)")
        return []
    finally:
        tmp.unlink(missing_ok=True)
        tmp.parent.rmdir()

    if proc.returncode != 0:
        log.warning("  (DESCRIBE TABLE failed - emitting untyped navigation: %s)", proc.stderr.strip()[:160])
        return []
    payload = json.loads(proc.stdout)
    if payload.get("status", {}).get("state") != "SUCCEEDED":
        log.warning("  (DESCRIBE TABLE not SUCCEEDED - emitting untyped navigation)")
        return []
    rows = payload.get("result", {}).get("data_array", []) or []
    columns = []
    for row in rows:
        name = (row[0] or "").strip()
        # DESCRIBE emits a blank line then partition metadata; stop at the first non-column row.
        if not name or name.startswith("#"):
            break
        columns.append((name, row[1] or ""))
    return columns


def _databricks_m(conn: dict, table: dict, columns: list[tuple[str, str]]) -> str:
    """Build the Databricks.Catalogs navigation M for one table.

    The navigation keys are `Name` + `Kind`, and the catalog level's Kind is "Database" (not
    "Catalog"), which is the single most-missed detail. Verified first-hand against a live warehouse
    and recorded in docs/data-source-credentials.md.
    """
    host = conn.get("server")
    http_path = conn.get("http_path")
    catalog = conn.get("database")
    schema = conn.get("schema")
    missing = [k for k, v in (("server", host), ("http_path", http_path), ("database", catalog)) if not v]
    if missing:
        return (
            f"// CANNOT EMIT: the spec's connection is missing {', '.join(missing)}.\n"
            "// A spec parsed before those fields existed must be re-parsed (scripts/parse_tableau.py)."
        )
    name = table.get("name")
    steps = [
        f'    Source = Databricks.Catalogs("{host}", "{http_path}", '
        "[Catalog=null, Database=null, EnableAutomaticProxyDiscovery=null]),",
        f'    CatalogLevel = Source{{[Name="{catalog}", Kind="Database"]}}[Data],',
    ]
    last = "CatalogLevel"
    if schema:
        steps.append(f'    SchemaLevel = CatalogLevel{{[Name="{schema}", Kind="Schema"]}}[Data],')
        last = "SchemaLevel"
    steps.append(f'    TableLevel = {last}{{[Name="{name}", Kind="Table"]}}[Data]')
    final = "TableLevel"
    if columns:
        typed = [f'{{"{c}", {_m_type(t)}}}' for c, t in columns if _m_type(t)]
        if typed:
            steps[-1] += ","
            steps.append(f'    Typed = Table.TransformColumnTypes(TableLevel, {{{", ".join(typed)}}}, "en-US")')
            final = "Typed"
    return "let\n" + "\n".join(steps) + f"\nin\n    {final}"


def _flat_file_m(table: dict, param: str) -> str:
    """Build the DataFolder-parameter CSV M for a flat-file source."""
    name = table.get("name") or "data.csv"
    if not name.lower().endswith((".csv", ".txt")):
        name = f"{name}.csv"
    return (
        "let\n"
        f'    Source = Csv.Document(File.Contents({param} & "{name}"), '
        '[Delimiter=",", Encoding=65001, QuoteStyle=QuoteStyle.Csv]),\n'
        "    Promoted = Table.PromoteHeaders(Source, [PromoteAllScalars=true])\n"
        "in\n"
        "    Promoted"
    )


def emit(spec: dict, only: str | None, introspect: bool, warehouse: str | None, param: str) -> int:
    """Print the M for every (or one) data source. Returns the number of sources it could not emit."""
    failed = 0
    for source in spec.get("data_sources", []):
        caption = source.get("caption") or source.get("id")
        if only and only not in (caption, source.get("id"), source.get("internal_name")):
            continue
        conn = source.get("connection", {}) or {}
        target = conn.get("powerbi_target")
        tables = source.get("tables") or [{}]
        log.info("")
        log.info("=" * 78)
        log.info("# %s   [%s / %s]", caption, conn.get("class"), target)
        log.info("=" * 78)
        for table in tables:
            if target == "flat_file":
                log.info("%s", _flat_file_m(table, param))
                continue
            if conn.get("class") == "databricks":
                columns = []
                if introspect:
                    wh = warehouse or (conn.get("http_path") or "").rsplit("/", 1)[-1]
                    qualified = (
                        (table.get("qualified_name") or table.get("name") or "").replace("[", "").replace("]", "")
                    )
                    log.info("// DESCRIBE TABLE %s (warehouse %s)", qualified, wh)
                    columns = describe_databricks_table(wh, qualified)
                    log.info("// %d column(s) read from the live source", len(columns))
                log.info("%s", _databricks_m(conn, table, columns))
                continue
            failed += 1
            log.info(
                "// NO VERIFIED M PATTERN for connection class '%s'.\n"
                "// Do NOT guess a connector's navigation shape - research it against Microsoft Learn,\n"
                "// verify it once, then add it here so the next migration gets it for free.",
                conn.get("class"),
            )
    return failed


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True, type=Path)
    ap.add_argument("--source", help="only this data source (caption or id)")
    ap.add_argument("--introspect", action="store_true", help="read real column types from the live source")
    ap.add_argument("--warehouse", help="override the Databricks warehouse id used for introspection")
    ap.add_argument("--data-folder-param", default="DataFolder", help="M parameter name for flat files")
    args = ap.parse_args(argv)

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    failed = emit(spec, args.source, args.introspect, args.warehouse, args.data_folder_param)
    if failed:
        log.info("")
        log.warning("%d data source(s) have no verified M pattern - see the comments above.", failed)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
