"""Regression tests for scripts/extract_hyper_data.py."""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from tableauhyperapi import (
    Connection,
    CreateMode,
    HyperProcess,
    Inserter,
    SqlType,
    TableDefinition,
    TableName,
    Telemetry,
)


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "extract_hyper_data.py"


def _create_hyper(path: Path, tables: dict[str, tuple[list[str], list[tuple[object, ...]]]]) -> None:
    """Create one synthetic Hyper extract with the requested tables."""
    log_dir = path.parent / "hyper_logs"
    log_dir.mkdir(exist_ok=True)
    with HyperProcess(
        telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_dir": str(log_dir)}
    ) as hyper:
        with Connection(
            endpoint=hyper.endpoint, database=str(path), create_mode=CreateMode.CREATE_AND_REPLACE
        ) as connection:
            connection.catalog.create_schema("Extract")
            for table_name, (columns, rows) in tables.items():
                table = TableDefinition(
                    TableName("Extract", table_name),
                    [TableDefinition.Column(column, SqlType.text()) for column in columns],
                )
                connection.catalog.create_table(table)
                with Inserter(connection, table) as inserter:
                    inserter.add_rows(rows)
                    inserter.execute()


def _csv_row_count(path: Path) -> int:
    """Count data rows in a CSV with a header."""
    with path.open(newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def test_export_and_enrich_cover_every_relation_across_every_extract(tmp_path: Path) -> None:
    """A federated multi-relation workbook must not silently keep only one relation per datasource. ds.small reuses
    big's `"Extract"."Products.csv_DIM"` on purpose: distinct data sources must not dedupe by qualified name."""
    big_hyper = tmp_path / "big.hyper"
    small_hyper = tmp_path / "small.hyper"
    _create_hyper(
        big_hyper,
        {
            "Orders.csv_FACT": (["Order ID", "Sales"], [("o1", "10"), ("o2", "20"), ("o3", "30")]),
            "Products.csv_DIM": (["Product ID"], [("p1",), ("p2",)]),
        },
    )
    _create_hyper(small_hyper, {"Products.csv_DIM": (["Product ID"], [("c1",), ("c2",), ("c3",), ("c4",)])})

    workbook = tmp_path / "multi_extract.twbx"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.write(big_hyper, "Data/big/federated.hyper")
        archive.write(small_hyper, "Data/small/federated.hyper")

    def table(name: str) -> dict:
        return {"id": f"tbl.{name}", "name": name, "row_count": {"source": "unknown"}}

    spec = tmp_path / "migration-spec.json"
    spec.write_text(
        json.dumps(
            {
                "data_sources": [
                    {
                        "id": "ds.big",
                        "connection": {"mode": "extract", "hyper_file": "Data/big/federated.hyper"},
                        "joins": [{"left": "Orders.csv_FACT", "right": "Products.csv_DIM", "type": "inner"}],
                        "tables": [table("Orders.csv_FACT"), table("Products.csv_DIM")],
                    },
                    {
                        "id": "ds.small",
                        "connection": {"mode": "extract", "hyper_file": "Data/small/federated.hyper"},
                        "joins": [],
                        "tables": [table("Sales")],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    output_dir = tmp_path / "out"
    subprocess.run(
        [sys.executable, str(SCRIPT), str(workbook), str(spec), "-o", str(output_dir)],
        check=True,
        cwd=SCRIPT.parents[1],
    )

    manifest = json.loads((output_dir / "extract_manifest.json").read_text(encoding="utf-8"))
    big_relations = {Path(relation["csv_path"]).name: relation for relation in manifest["ds.big"]["relations"]}
    all_relations = {
        Path(relation["csv_path"]).name: relation
        for data_source in manifest.values()
        for relation in data_source["relations"]
    }

    assert manifest["ds.big"]["relation_count"] == 2
    assert manifest["ds.big"]["total_row_count"] == 5
    assert manifest["ds.big"]["joins"] == [{"left": "Orders.csv_FACT", "right": "Products.csv_DIM", "type": "inner"}]
    assert len(all_relations) == 3
    assert big_relations["ds.big.Extract.Orders.csv_FACT.csv"]["row_count"]["value"] == 3
    assert big_relations["ds.big.Extract.Orders.csv_FACT.csv"]["row_count"]["source"] == "hyper"
    assert big_relations["ds.big.Extract.Orders.csv_FACT.csv"]["row_count"]["observed_at"].endswith("Z")
    small = manifest["ds.small"]["relations"]
    assert [relation["qualified_name"] for relation in small] == ['"Extract"."Products.csv_DIM"']
    assert big_relations["ds.big.Extract.Products.csv_DIM.csv"]["qualified_name"] == small[0]["qualified_name"]
    assert _csv_row_count(output_dir / "ds.big.Extract.Orders.csv_FACT.csv") == 3
    assert _csv_row_count(output_dir / "ds.big.Extract.Products.csv_DIM.csv") == 2
    assert _csv_row_count(output_dir / "ds.small.csv") == 4

    enriched = tmp_path / "enriched.json"
    subprocess.run(
        [sys.executable, str(SCRIPT), str(workbook), str(spec), "--enrich-spec", "--enriched-output", str(enriched)],
        check=True,
        cwd=SCRIPT.parents[1],
    )

    counts = {
        f"{data_source['id']}/{tbl['name']}": tbl["row_count"]
        for data_source in json.loads(enriched.read_text(encoding="utf-8"))["data_sources"]
        for tbl in data_source["tables"]
    }
    assert {key: count["value"] for key, count in counts.items()} == {
        "ds.big/Orders.csv_FACT": 3,
        "ds.big/Products.csv_DIM": 2,
        "ds.small/Sales": 4,
    }
    assert all(count["source"] == "hyper" and count["observed_at"].endswith("Z") for count in counts.values())


def _load_script_module():
    """Import scripts/extract_hyper_data.py directly; scripts/ is not an importable package."""
    spec = importlib.util.spec_from_file_location("extract_hyper_data_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_duplicate_unqualified_table_names_are_omitted_rather_than_last_write_wins() -> None:
    """Two schemas holding the same table name must yield no count, not a confidently wrong one. THREE duplicates,
    not two, on purpose. Refs PR #232."""
    module = _load_script_module()
    relations = [
        {"table": "Orders", "row_count": {"value": 3, "source": "hyper", "observed_at": "Z"}},
        {"table": "Orders", "row_count": {"value": 42104338, "source": "hyper", "observed_at": "Z"}},
        {"table": "Orders", "row_count": {"value": 99, "source": "hyper", "observed_at": "Z"}},
        {"table": "Products", "row_count": {"value": 7, "source": "hyper", "observed_at": "Z"}},
    ]

    counts = module._row_count_by_table_name(relations)

    assert "orders" not in counts, "an ambiguous name must be omitted, not resolved by iteration order"
    assert counts["products"]["value"] == 7


def test_a_null_hyper_file_is_reported_not_raised(tmp_path: Path) -> None:
    """parse_tableau emits hyper_file: null when <extract><connection> carries no dbname."""
    module = _load_script_module()
    migration_spec = {
        "data_sources": [
            {"id": "ds.orders", "connection": {"class": "hyper", "mode": "extract", "hyper_file": None}, "tables": []}
        ]
    }

    manifest = module.extract_data_sources(migration_spec, {}, tmp_path / "out")

    assert "hyper file not found" in manifest["ds.orders"]["error"]


def test_schema_scratch_never_lands_beside_the_workbook(tmp_path: Path, monkeypatch) -> None:
    """Extracted .hyper residue under migrations/<tree>/<slug>/source/ is NOT gitignored (issue #125). Observes the
    scratch path WHILE IT EXISTS rather than asserting on source text."""
    module = _load_script_module()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    hyper = tmp_path / "orders.hyper"
    _create_hyper(hyper, {"Orders": (["id"], [("1",), ("2",), ("3",)])})
    workbook = source_dir / "wb.twbx"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.write(hyper, "Data/orders/orders.hyper")

    created: list[Path] = []

    class _Recording(tempfile.TemporaryDirectory):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(Path(self.name).resolve())

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", _Recording)
    monkeypatch.setattr(sys, "argv", ["extract_hyper_data.py", str(workbook), "--schema"])
    module.main()

    assert created, "no scratch dir was created - the test proved nothing"
    assert all(not scratch.is_relative_to(workbook.parent.resolve()) for scratch in created), (
        f"scratch extraction must stay in the OS temp dir, got {created}: a TemporaryDirectory "
        "beside the workbook leaves customer .hyper data in an unignored path, and it is not "
        "cleaned up if the process is killed"
    )
