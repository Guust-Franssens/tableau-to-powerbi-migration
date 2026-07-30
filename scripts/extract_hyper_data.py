"""
purpose: Inspect, and where appropriate extract, the .hyper file(s) packaged inside a Tableau .twbx.

         --schema : describe tables/columns/row-counts, exporting NO rows. Always safe, and the
                    ONLY appropriate Hyper access for a live source. Use it instead of hand-rolling
                    a throwaway tableauhyperapi script.

         default  : export one CSV per migration-spec.json data source, so a FLAT-FILE source
                    (Excel/CSV/JSON - `connection.powerbi_target == "flat_file"`) becomes a
                    self-contained model that shows real numbers in Power BI Desktop.

         IMPORTANT: a .hyper is Tableau's CACHE. When the original source is a live system
         (`powerbi_target == "live_source"`: Snowflake, SQL Server, Databricks, ...) the semantic
         model must CONNECT to that system, exactly as Tableau does. Exporting the cached rows
         freezes the data at export time and produces a model that can never refresh - it looks
         correct on day one and is quietly broken. The default mode warns when it detects this.
usage:   python scripts/extract_hyper_data.py <workbook.twbx> --schema
         python scripts/extract_hyper_data.py <workbook.twbx> <migration-spec.json> -o <output_dir>
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from tableauhyperapi import Connection, HyperProcess, Telemetry

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("extract_hyper_data")


def extract_hyper_files(workbook_path: Path, dest_dir: Path) -> dict[str, Path]:
    """Unzip every packaged .hyper file from the .twbx into dest_dir. Returns {file_name: extracted_path}."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: dict[str, Path] = {}
    with zipfile.ZipFile(workbook_path) as zf:
        for entry in zf.namelist():
            if not entry.lower().endswith(".hyper"):
                continue
            file_name = Path(entry).name
            out_path = dest_dir / file_name
            out_path.write_bytes(zf.read(entry))
            extracted[file_name] = out_path
    logger.info("Extracted %d .hyper file(s) to %s", len(extracted), dest_dir)
    return extracted


def _quote_ident(name: str) -> str:
    """Quote a Hyper identifier (table/column name) for safe interpolation into SQL."""
    return '"' + name.replace('"', '""') + '"'


def export_table_to_csv(connection: Connection, csv_path: Path) -> int:
    """Export the first (and only expected) table in the Hyper file's extract schema to CSV. Returns
    the row count exported.

    Note: SchemaName.__str__ returns a SQL-quoted form (e.g. '"public"'), not the bare name, so schema
    selection is done by finding the first schema that actually contains a table rather than by
    string-matching a schema name."""
    table = next(
        (t for s in connection.catalog.get_schema_names() for t in connection.catalog.get_table_names(schema=s)),
        None,
    )
    if table is None:
        raise ValueError("No tables found in any schema")
    table_def = connection.catalog.get_table_definition(table)
    columns = [c.name.unescaped for c in table_def.columns]

    quoted_cols = ", ".join(_quote_ident(c) for c in columns)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    connection.execute_command(
        f"COPY (SELECT {quoted_cols} FROM {table}) TO '{csv_path.as_posix()}' "
        "WITH (FORMAT CSV, HEADER TRUE, DELIMITER ',')"
    )
    return connection.execute_scalar_query(f"SELECT COUNT(*) FROM {table}")


def extract_data_sources(migration_spec: dict[str, Any], hyper_dir: Path, output_dir: Path) -> dict[str, Any]:
    """For every extract-based data source in the spec, export its Hyper table to a CSV named after the
    data source id. Returns a manifest {ds_id: {csv_path, row_count}} plus records any failures inline."""
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}

    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        for ds in migration_spec["data_sources"]:
            connection_info = ds["connection"]
            if connection_info["mode"] != "extract":
                continue
            hyper_file_name = Path(connection_info.get("hyper_file", "")).name
            hyper_path = hyper_dir / hyper_file_name
            if not hyper_path.exists():
                logger.warning("Hyper file not found for %s: %s", ds["id"], hyper_path)
                manifest[ds["id"]] = {"error": f"hyper file not found: {hyper_path}"}
                continue

            csv_path = output_dir / f"{ds['id']}.csv"
            with Connection(endpoint=hyper.endpoint, database=str(hyper_path)) as connection:
                row_count = export_table_to_csv(connection, csv_path)
            manifest[ds["id"]] = {"csv_path": str(csv_path), "row_count": row_count}
            logger.info("Exported %s -> %s (%d rows)", ds["id"], csv_path, row_count)

    return manifest


def describe_schema(workbook_path: Path, hyper_dir: Path) -> list[dict[str, Any]]:
    """Every table + column in the packaged .hyper files, WITHOUT exporting a single row.

    Exists so nobody hand-rolls a throwaway `tableauhyperapi` script just to see what is in an
    extract - a real user watched an agent do exactly that. It is also the *only* Hyper access that
    is appropriate for a `live_source` migration: there you need the SCHEMA (to build the model
    against the real upstream) and a validation baseline, never the cached rows.
    """
    tables: list[dict[str, Any]] = []
    hyper_files = extract_hyper_files(workbook_path, hyper_dir)
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        for name, path in sorted(hyper_files.items()):
            with Connection(endpoint=hyper.endpoint, database=path) as connection:
                for schema in connection.catalog.get_schema_names():
                    for table in connection.catalog.get_table_names(schema=schema):
                        definition = connection.catalog.get_table_definition(table)
                        rows = connection.execute_scalar_query(f"SELECT COUNT(*) FROM {table}")
                        tables.append(
                            {
                                "hyper_file": name,
                                "schema": str(schema),
                                "table": str(table.name),
                                "row_count": rows,
                                "columns": [
                                    {"name": c.name.unescaped, "type": str(c.type), "nullable": str(c.nullability)}
                                    for c in definition.columns
                                ],
                            }
                        )
    return tables


def main() -> None:
    """CLI entry point: extract packaged .hyper data and write one CSV per extract-based data source."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workbook", type=Path, help="Path to the .twbx file (must contain packaged .hyper files)")
    parser.add_argument(
        "migration_spec",
        type=Path,
        nargs="?",
        help="Path to migration-spec.json produced by parse_tableau.py (not needed with --schema)",
    )
    parser.add_argument("-o", "--output", type=Path, help="Output directory for CSV files (required without --schema)")
    parser.add_argument(
        "--schema",
        action="store_true",
        help="Describe tables/columns/row-counts only, exporting NO rows. Use this for a live_source "
        "migration, where the .hyper is Tableau's cache and the model must connect upstream instead.",
    )
    args = parser.parse_args()

    if args.schema:
        with tempfile.TemporaryDirectory(prefix="hyper_schema_") as scratch:
            tables = describe_schema(args.workbook, Path(scratch))
        print(json.dumps({"hyper_tables": tables}, indent=2))
        logger.info(
            "Schema only - no rows exported. %d table(s). If this data source's "
            "connection.powerbi_target is 'live_source', build the model against the UPSTREAM system; "
            "these cached rows are a validation baseline, not the model's source.",
            len(tables),
        )
        return

    if args.migration_spec is None or args.output is None:
        parser.error("migration_spec and -o/--output are required unless --schema is given")

    migration_spec = json.loads(args.migration_spec.read_text(encoding="utf-8"))
    _warn_on_live_sources(migration_spec)
    hyper_dir = args.output / "_hyper_raw"
    extract_hyper_files(args.workbook, hyper_dir)
    manifest = extract_data_sources(migration_spec, hyper_dir, args.output)

    manifest_path = args.output / "extract_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Wrote manifest to %s", manifest_path)


def _warn_on_live_sources(migration_spec: dict[str, Any]) -> None:
    """Loudly flag extracting rows for a source the model should be connecting to instead.

    Not a hard failure: a user may deliberately want an offline snapshot. But it must never happen
    by accident, because the resulting model looks correct on day one and can never refresh.
    """
    live = [
        ds["id"]
        for ds in migration_spec.get("data_sources", [])
        if (ds.get("connection") or {}).get("powerbi_target") == "live_source"
    ]
    if live:
        logger.warning(
            "WARNING: %d data source(s) are LIVE systems, not files: %s\n"
            "  Exporting their cached rows is NOT the faithful migration - the semantic model should\n"
            "  CONNECT to the upstream system, exactly as Tableau does. These CSVs freeze the data at\n"
            "  export time and the model can never refresh.\n"
            "  Use `--schema` for schema discovery instead, and only continue if you deliberately want\n"
            "  an offline snapshot.",
            len(live),
            ", ".join(live),
        )


if __name__ == "__main__":
    main()
