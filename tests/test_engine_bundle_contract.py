"""Regression tests for gate tools consuming deterministic-engine bundles without fake specs."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO / "scripts" / "preflight_source_credentials.py"
PROBE = REPO / "scripts" / "probe_live_source.py"
REGISTRY = REPO / "scripts" / "published_datasource_registry.py"
GATE = REPO / "scripts" / "credential_gate.py"


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


def test_preflight_accepts_engine_bundle_and_arms_the_real_bundle_dir(tmp_path: Path) -> None:
    """#55: the classifier must not require a hand-made migration-spec.json scratch tree."""
    bundle = _write_engine_bundle(tmp_path / "engine", [_live_embedded_source()])
    try:
        proc = subprocess.run(
            [sys.executable, str(PREFLIGHT), "--bundle", str(bundle)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 1
        assert (bundle / ".credential-gate-BLOCKED.json").is_file()
        assert not (bundle / "migration-spec.json").exists()
    finally:
        subprocess.run(
            [sys.executable, str(GATE), "clear", str(bundle), "--reason", "test-teardown"],
            capture_output=True,
            text=True,
            check=False,
        )


def test_preflight_refuses_an_engine_bundle_without_source_contract(tmp_path: Path) -> None:
    """Absence must stay visible; a plausible empty spec would be the old anti-pattern."""
    bundle = _write_engine_bundle(tmp_path / "engine")
    proc = subprocess.run(
        [sys.executable, str(PREFLIGHT), "--bundle", str(bundle)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "No explicit data_sources" in (proc.stdout + proc.stderr)


def test_probe_accepts_engine_bundle_but_refuses_missing_probe_targets(tmp_path: Path) -> None:
    """The probe has a native bundle entry point, but it still refuses to invent tables/columns."""
    bundle = _write_engine_bundle(tmp_path / "engine", [_live_embedded_source()])
    proc = subprocess.run(
        [sys.executable, str(PROBE), "--bundle", str(bundle), "--timeout-sec", "1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "source has no table/column to probe" in (proc.stdout + proc.stderr)


def test_registry_accepts_engine_binding_signal_by_name(tmp_path: Path) -> None:
    """#55: `binding_signal.recommendation` is a first-class published-datasource signal."""
    bundle = _write_engine_bundle(tmp_path / "engine", published_name="SalesMaster", binding_key="finance/salesmaster")
    proc = subprocess.run(
        [
            sys.executable,
            str(REGISTRY),
            "--bundle",
            str(bundle),
            "--migrations-dir",
            str(tmp_path / "workbooks"),
            "--datasources-dir",
            str(tmp_path / "datasources"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "finance/salesmaster" in (proc.stdout + proc.stderr)


def test_registry_preserves_engine_published_signal_with_unknown_key(tmp_path: Path) -> None:
    """The engine emits a name, not a stable key; unknown must not collapse to empty."""
    bundle = _write_engine_bundle(tmp_path / "engine", published_name="SalesMaster")
    proc = subprocess.run(
        [
            sys.executable,
            str(REGISTRY),
            "--bundle",
            str(bundle),
            "--migrations-dir",
            str(tmp_path / "workbooks"),
            "--datasources-dir",
            str(tmp_path / "datasources"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1
    assert "UNKNOWN key" in out
    assert "No published (sqlproxy)" not in out


def test_registry_preserves_review_rebind_published_signal_with_unknown_key(tmp_path: Path) -> None:
    """`review_rebind` still means a published datasource exists; only the action differs."""
    bundle = _write_engine_bundle(tmp_path / "engine", published_name="Superstore (Published)")
    report = json.loads((bundle / "report.json").read_text(encoding="utf-8"))
    report["workbooks"][0]["binding_signal"]["recommendation"] = "review_rebind"
    (bundle / "report.json").write_text(json.dumps(report), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(REGISTRY),
            "--bundle",
            str(bundle),
            "--migrations-dir",
            str(tmp_path / "workbooks"),
            "--datasources-dir",
            str(tmp_path / "datasources"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1
    assert "UNKNOWN key" in out
    assert "Superstore (Published)" in out
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
    """Readable contracts with no published sources are not the same as an empty scan."""
    migrations = tmp_path / "workbooks"
    spec_dir = migrations / "embedded"
    spec_dir.mkdir(parents=True)
    (spec_dir / "migration-spec.json").write_text(
        json.dumps({"data_sources": [{"name": "Flat", "connection": {"class": "excel-direct"}}]}),
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(REGISTRY), "--scan", "--migrations-dir", str(migrations)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "All parsed workbooks use embedded data sources" in (proc.stdout + proc.stderr)
