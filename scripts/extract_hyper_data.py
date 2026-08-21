"""
purpose: Inspect, and where appropriate extract, the .hyper file(s) packaged inside a Tableau .twbx.

         --schema : describe tables/columns/row-counts, exporting NO rows. Always safe, and the
                    ONLY appropriate Hyper access for a live source. Use it instead of hand-rolling
                    a throwaway tableauhyperapi script.

         default  : export one CSV per Hyper relation in every migration-spec.json extract data source,
                    so a FLAT-FILE source (Excel/CSV/JSON - `connection.powerbi_target == "flat_file"`)
                    becomes a self-contained model that shows real numbers in Power BI Desktop.

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
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tableauhyperapi import Connection, HyperProcess, Telemetry

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("extract_hyper_data")


def extract_hyper_files(workbook_path: Path, dest_dir: Path) -> dict[str, Path]:
    """Unzip every packaged .hyper file from the .twbx into dest_dir.

    Returns {archive_member: extracted_path}. Extracted file names include the archive member path so
    two same-named packaged extracts cannot overwrite one another.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    extracted: dict[str, Path] = {}
    with zipfile.ZipFile(workbook_path) as zf:
        for entry in zf.namelist():
            if not entry.lower().endswith(".hyper"):
                continue
            out_path = dest_dir / f"{len(extracted):03d}_{_safe_name(entry)}"
            out_path.write_bytes(zf.read(entry))
            extracted[entry] = out_path
    logger.info("Extracted %d .hyper file(s) to %s", len(extracted), dest_dir)
    return extracted


def _safe_name(value: object) -> str:
    """Return a filesystem-safe stem for Hyper archive members or qualified table names."""
    raw = str(value).replace('"', "").replace("[", "").replace("]", "")
    cleaned = re.sub(r"[^0-9A-Za-z _.-]+", "_", raw.replace("\\", "_").replace("/", "_")).strip(" ._")
    return cleaned or "table"


def _quote_ident(name: str) -> str:
    """Quote a Hyper identifier (table/column name) for safe interpolation into SQL."""
    return '"' + name.replace('"', '""') + '"'


def _utc_now_z() -> str:
    """Return a JSON-schema date-time timestamp with a literal UTC Z suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _hyper_row_count(value: object, observed_at: str) -> dict[str, Any]:
    """Wrap a Hyper count with provenance so stale extract counts are never mistaken for live facts."""
    return {"value": int(value), "source": "hyper", "observed_at": observed_at}


def _copy_sql(table: object, columns: list[str], csv_path: Path) -> str:
    """Build the Hyper COPY command for a table export."""
    quoted_cols = ", ".join(_quote_ident(c) for c in columns)
    escaped_path = csv_path.as_posix().replace("'", "''")
    return f"COPY (SELECT {quoted_cols} FROM {table}) TO '{escaped_path}' WITH (FORMAT CSV, HEADER TRUE, DELIMITER ',')"


def export_tables_to_csv(
    connection: Connection, output_dir: Path, observed_at: str | None = None
) -> dict[str, dict[str, Any]]:
    """Export every table in one Hyper file to CSV, keyed by qualified table name."""
    observed_at = observed_at or _utc_now_z()
    results: dict[str, dict[str, Any]] = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    for schema in connection.catalog.get_schema_names():
        for table in connection.catalog.get_table_names(schema=schema):
            table_def = connection.catalog.get_table_definition(table)
            columns = [c.name.unescaped for c in table_def.columns]
            csv_path = output_dir / f"{_safe_name(table)}.csv"
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            connection.execute_command(_copy_sql(table, columns, csv_path))
            results[str(table)] = {
                "csv_path": str(csv_path.resolve()),
                "columns": columns,
                "row_count": _hyper_row_count(
                    connection.execute_scalar_query(f"SELECT COUNT(*) FROM {table}"), observed_at
                ),
            }
    if not results:
        raise ValueError("No tables found in any schema")
    return results


def _resolve_hyper_path(hyper_files: dict[str, Path], hyper_file: str) -> Path | None:
    """Find an extracted Hyper member by exact archive path first, then unique basename."""
    if hyper_file in hyper_files:
        return hyper_files[hyper_file]
    matches = [path for member, path in hyper_files.items() if Path(member).name == Path(hyper_file).name]
    return matches[0] if len(matches) == 1 else None


def _manifest_relation(qualified_name: str, info: dict[str, Any]) -> dict[str, Any]:
    """Format one exported relation for extract_manifest.json."""
    return {
        "qualified_name": qualified_name,
        "csv_path": info["csv_path"],
        "row_count": info["row_count"],
        "columns": info["columns"],
    }


def _assert_no_silent_loss(ds_id: str, relation_count: int, relations: list[dict[str, Any]]) -> None:
    """Fail if the manifest would hide that fewer CSV files exist than Hyper relations."""
    csv_paths = {relation["csv_path"] for relation in relations}
    if len(csv_paths) != relation_count:
        raise RuntimeError(
            f"{ds_id} has {relation_count} relation(s), but only {len(csv_paths)} CSV file(s) were written"
        )


def _relation_csv_path(output_dir: Path, ds_id: str, qualified_name: str, relation_count: int) -> Path:
    """Keep the old single-relation filename contract; namespace only multi-relation exports."""
    if relation_count == 1:
        return output_dir / f"{_safe_name(ds_id)}.csv"
    return output_dir / f"{_safe_name(ds_id)}.{_safe_name(qualified_name)}.csv"


def _export_hyper_file(
    hyper_path: Path,
    output_dir: Path,
    ds_id: str,
    hyper: HyperProcess,
    observed_at: str,
) -> dict[str, dict[str, Any]]:
    """Export one data source's Hyper file without deduping across other extracts."""
    with tempfile.TemporaryDirectory(prefix="hyper_csv_", dir=output_dir) as stage:
        with Connection(endpoint=hyper.endpoint, database=str(hyper_path)) as connection:
            staged = export_tables_to_csv(connection, Path(stage), observed_at)
        exported: dict[str, dict[str, Any]] = {}
        for qualified_name, info in staged.items():
            final_csv = _relation_csv_path(output_dir, ds_id, qualified_name, len(staged))
            shutil.copyfile(info["csv_path"], final_csv)
            exported[qualified_name] = {**info, "csv_path": str(final_csv.resolve())}
        return exported


def extract_data_sources(
    migration_spec: dict[str, Any],
    hyper_files: dict[str, Path],
    output_dir: Path,
) -> dict[str, Any]:
    """Export every relation in every extract-backed data source and return a manifest by data source."""
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {}
    observed_at = _utc_now_z()

    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        for ds in migration_spec["data_sources"]:
            connection_info = ds["connection"]
            if connection_info["mode"] != "extract":
                continue
            # Normalise once: the spec schema allows a null hyper_file, and Path(None) raises.
            hyper_file_value = connection_info.get("hyper_file") or ""
            hyper_file_name = Path(hyper_file_value).name
            hyper_path = _resolve_hyper_path(hyper_files, hyper_file_value)
            if hyper_path is None:
                logger.warning("Hyper file not found for %s: %s", ds["id"], hyper_file_name)
                manifest[ds["id"]] = {"error": f"hyper file not found: {hyper_file_name}"}
                continue

            exported = _export_hyper_file(hyper_path, output_dir, ds["id"], hyper, observed_at)
            relations = [_manifest_relation(name, info) for name, info in sorted(exported.items())]
            _assert_no_silent_loss(ds["id"], len(exported), relations)
            manifest[ds["id"]] = {
                "hyper_file": connection_info.get("hyper_file"),
                "joins": ds.get("joins", []),
                "relation_count": len(relations),
                "total_row_count": sum(relation["row_count"]["value"] for relation in relations),
                "relations": relations,
            }
            logger.info(
                "Exported %s -> %d relation(s), %d row(s)",
                ds["id"],
                len(relations),
                manifest[ds["id"]]["total_row_count"],
            )

    return manifest


def _describe_hyper_file(connection: Connection, hyper_file: str, observed_at: str) -> list[dict[str, Any]]:
    """Describe every table in one open Hyper file with provenance-tagged row counts."""
    tables: list[dict[str, Any]] = []
    for schema in connection.catalog.get_schema_names():
        for table in connection.catalog.get_table_names(schema=schema):
            definition = connection.catalog.get_table_definition(table)
            rows = connection.execute_scalar_query(f"SELECT COUNT(*) FROM {table}")
            tables.append(
                {
                    "hyper_file": hyper_file,
                    "schema": str(schema),
                    "table": str(table.name),
                    "qualified_name": str(table),
                    "row_count": _hyper_row_count(rows, observed_at),
                    "columns": [
                        {"name": c.name.unescaped, "type": str(c.type), "nullable": str(c.nullability)}
                        for c in definition.columns
                    ],
                }
            )
    return tables


def describe_schema(workbook_path: Path, hyper_dir: Path) -> list[dict[str, Any]]:
    """Every table + column in the packaged .hyper files, WITHOUT exporting a single row.

    Exists so nobody hand-rolls a throwaway `tableauhyperapi` script just to see what is in an
    extract - a real user watched an agent do exactly that. It is also the *only* Hyper access that
    is appropriate for a `live_source` migration: there you need the SCHEMA (to build the model
    against the real upstream) and a validation baseline, never the cached rows.
    """
    tables: list[dict[str, Any]] = []
    hyper_files = extract_hyper_files(workbook_path, hyper_dir)
    observed_at = _utc_now_z()
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        for name, path in sorted(hyper_files.items()):
            with Connection(endpoint=hyper.endpoint, database=path) as connection:
                tables.extend(_describe_hyper_file(connection, name, observed_at))
    return tables


def _normalise_table_name(value: str | None) -> str:
    """Normalise Tableau relation names and Hyper table names for best-effort matching."""
    cleaned = (value or "").replace('"', "").replace("[", "").replace("]", "").strip()
    return re.sub(r"\s+", " ", cleaned).casefold()


def _row_count_by_table_name(relations: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return unique Hyper counts keyed by unqualified table name; ambiguous names are omitted."""
    counts: dict[str, dict[str, Any]] = {}
    ambiguous: set[str] = set()
    for relation in relations:
        key = _normalise_table_name(relation.get("table"))
        if not key or key in ambiguous or key in counts:
            ambiguous.add(key)
            counts.pop(key, None)
            continue
        counts[key] = relation["row_count"]
    return counts


def _apply_hyper_counts_to_tables(ds: dict[str, Any], relations: list[dict[str, Any]]) -> int:
    """Attach Hyper counts to spec tables and return the number of tables updated."""
    tables = ds.get("tables") or []
    if not tables or not relations:
        return 0
    if len(tables) == 1 and len(relations) == 1:
        tables[0]["row_count"] = relations[0]["row_count"]
        return 1

    by_name = _row_count_by_table_name(relations)
    updated = 0
    for table in tables:
        row_count = by_name.get(_normalise_table_name(table.get("name")))
        if row_count is None:
            continue
        table["row_count"] = row_count
        updated += 1
    return updated


def enrich_spec_with_hyper_counts(
    migration_spec: dict[str, Any],
    hyper_files: dict[str, Path],
) -> tuple[dict[str, Any], int]:
    """Return a copy of migration_spec enriched with per-table Hyper row-count hints.

    This is deliberately opt-in rather than part of parse_tableau.py: opening every packaged `.hyper`
    during every parse would make the offline parser slower and dependent on tableauhyperapi.
    """
    enriched = json.loads(json.dumps(migration_spec))
    updated = 0
    observed_at = _utc_now_z()
    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        for ds in enriched.get("data_sources", []):
            connection_info = ds.get("connection") or {}
            if connection_info.get("mode") != "extract":
                continue
            hyper_path = _resolve_hyper_path(hyper_files, connection_info.get("hyper_file") or "")
            if hyper_path is None:
                logger.warning("Hyper file not found for %s: %s", ds.get("id"), connection_info.get("hyper_file"))
                continue
            with Connection(endpoint=hyper.endpoint, database=str(hyper_path)) as connection:
                relations = _describe_hyper_file(connection, connection_info.get("hyper_file") or "", observed_at)
            updated += _apply_hyper_counts_to_tables(ds, relations)
    return enriched, updated


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
    parser.add_argument(
        "--enrich-spec",
        action="store_true",
        help="Update migration-spec.json with per-table Hyper row-count hints, exporting NO rows.",
    )
    parser.add_argument(
        "--enriched-output",
        type=Path,
        help="Where to write --enrich-spec output. Defaults to overwriting migration_spec in place.",
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

    if args.enrich_spec:
        if args.migration_spec is None:
            parser.error("migration_spec is required with --enrich-spec")
        migration_spec = json.loads(args.migration_spec.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="hyper_schema_") as scratch:
            hyper_files = extract_hyper_files(args.workbook, Path(scratch))
            enriched, updated = enrich_spec_with_hyper_counts(migration_spec, hyper_files)
        target = args.enriched_output or args.migration_spec
        target.write_text(json.dumps(enriched, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("Wrote %s with %d table row-count hint(s)", target, updated)
        return

    if args.migration_spec is None or args.output is None:
        parser.error("migration_spec and -o/--output are required unless --schema is given")

    migration_spec = json.loads(args.migration_spec.read_text(encoding="utf-8"))
    _warn_on_live_sources(migration_spec)
    hyper_dir = args.output / "_hyper_raw"
    hyper_files = extract_hyper_files(args.workbook, hyper_dir)
    manifest = extract_data_sources(migration_spec, hyper_files, args.output)

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
