"""Regression tests for gate tools consuming deterministic-engine bundles without fake specs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO / "scripts" / "preflight_source_credentials.py"
REGISTRY = REPO / "scripts" / "published_datasource_registry.py"


def _run(script: Path, args: list[str]) -> tuple[int, str]:
    proc = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout + proc.stderr


def _write_engine_bundle(
    root: Path,
    embedded: list[dict] | None = None,
    published_name: str | None = None,
    binding_key: str | None = None,
) -> Path:
    """Create the smallest native engine bundle shape the contract reader supports."""
    root.mkdir()
    (root / "input_manifest.json").write_text('{"inputs": []}\n', encoding="utf-8")
    (root / "report.json").write_text(
        json.dumps(
            {
                "tool": "tableau-fabric-skills",
                "workbooks": [
                    {
                        "name": "Sales",
                        "binding_signal": _binding_signal(published_name, binding_key),
                        "embedded_datasources": embedded or [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    handover = root / "handover"
    handover.mkdir()
    (handover / "Sales.json").write_text(
        json.dumps({"workbook": {"name": "Sales", "embedded_datasources": embedded or []}}),
        encoding="utf-8",
    )
    return root


def _binding_signal(published_name: str | None, binding_key: str | None) -> dict:
    signal = {
        "kind": "published",
        "recommendation": "candidate_rebind_to_published",
        "published_ds_name": published_name,
    }
    if binding_key:
        signal["published_datasource"] = {"key": binding_key}
    return signal if published_name or binding_key else {}


def _live_embedded_source() -> dict:
    return {
        "caption": "Warehouse",
        "label": "Warehouse",
        "connection_class": "snowflake",
        "named_connection_count": 1,
        "table_count": 3,
        "connections": [
            {
                "connection_class": "snowflake",
                "server": "acct.snowflakecomputing.com",
                "database": "DB",
                "schema": "PUBLIC",
                "warehouse": "WH",
            }
        ],
    }


def test_preflight_accepts_engine_bundle_and_classifies_it_read_only(tmp_path: Path) -> None:
    """#55: the classifier reads the real bundle (no scratch spec), reports the live source, writes nothing."""
    bundle = _write_engine_bundle(tmp_path / "engine", [_live_embedded_source()])
    before = {path: path.read_bytes() for path in bundle.rglob("*") if path.is_file()}
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT), "--bundle", str(bundle)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "PROOF REQUIRED - 1 live source(s)" in proc.stdout + proc.stderr
    assert {path: path.read_bytes() for path in bundle.rglob("*") if path.is_file()} == before


def test_preflight_refuses_an_engine_bundle_without_source_contract(tmp_path: Path) -> None:
    bundle = _write_engine_bundle(tmp_path / "engine")
    code, out = _run(PREFLIGHT, ["--bundle", str(bundle)])
    assert code == 2
    assert "No explicit data_sources" in out


@pytest.mark.parametrize(
    ("name", "key", "recommendation", "expected"),
    [
        ("SalesMaster", "finance/salesmaster", None, ["finance/salesmaster"]),
        ("SalesMaster", None, None, ["UNKNOWN key"]),
        ("Superstore (Published)", None, "review_rebind", ["UNKNOWN key", "Superstore (Published)"]),
    ],
)
def test_registry_keeps_engine_published_signals(tmp_path: Path, name, key, recommendation, expected) -> None:
    """#55: binding_signal is a first-class published signal; an unknown key never collapses to empty."""
    bundle = _write_engine_bundle(tmp_path / "engine", published_name=name, binding_key=key)
    if recommendation:
        report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
        report["workbooks"][0]["binding_signal"]["recommendation"] = recommendation
        (bundle / "report.json").write_text(json.dumps(report), encoding="utf-8")
    args = ["--bundle", str(bundle), "--migrations-dir", str(tmp_path / "w"), "--datasources-dir", str(tmp_path / "d")]
    code, out = _run(REGISTRY, args)
    assert code == 1
    for fragment in expected:
        assert fragment in out
    assert "No published (sqlproxy)" not in out


def test_registry_scan_refuses_to_pass_an_empty_scan(tmp_path: Path) -> None:
    """#55: an empty scan must not confidently report that no shared models are needed."""
    migrations = tmp_path / "workbooks"
    migrations.mkdir()
    proc = subprocess.run(
        [sys.executable, str(REGISTRY), "--scan", "--migrations-dir", str(migrations)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "Refusing to conclude" in (proc.stdout + proc.stderr)


def test_registry_scan_keeps_embedded_only_spec_path_as_success(tmp_path: Path) -> None:
    spec_dir = tmp_path / "workbooks" / "embedded"
    spec_dir.mkdir(parents=True)
    (spec_dir / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"name": "Flat", "connection": {"class": "excel-direct"}}]}), encoding="utf-8"
    )
    code, out = _run(REGISTRY, ["--scan", "--migrations-dir", str(tmp_path / "workbooks")])
    assert code == 0
    assert "All parsed workbooks use embedded data sources" in out
