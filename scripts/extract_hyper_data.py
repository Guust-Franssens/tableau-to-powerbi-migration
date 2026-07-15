"""
purpose: Extract row data from the .hyper file(s) packaged inside a Tableau .twbx workbook into CSVs,
         one per migration-spec.json data source. This materializes real data for extract-based
         (mode="extract") data sources so the generated Fabric semantic model is self-contained and
         does not need a live upstream connection to open and show real numbers in Power BI Desktop.
usage:   python scripts/extract_hyper_data.py <workbook.twbx> <migration-spec.json> -o <output_dir>
"""

from __future__ import annotations

import argparse
import json
import logging
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


def main() -> None:
    """CLI entry point: extract packaged .hyper data and write one CSV per extract-based data source."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path, help="Path to the .twbx file (must contain packaged .hyper files)")
    parser.add_argument("migration_spec", type=Path, help="Path to migration-spec.json produced by parse_tableau.py")
    parser.add_argument("-o", "--output", type=Path, required=True, help="Output directory for CSV files")
    args = parser.parse_args()

    migration_spec = json.loads(args.migration_spec.read_text(encoding="utf-8"))
    hyper_dir = args.output / "_hyper_raw"
    extract_hyper_files(args.workbook, hyper_dir)
    manifest = extract_data_sources(migration_spec, hyper_dir, args.output)

    manifest_path = args.output / "extract_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Wrote manifest to %s", manifest_path)


if __name__ == "__main__":
    main()
