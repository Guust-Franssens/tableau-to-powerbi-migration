"""Regression tests for the throwaway PBIP emitted by ``probe_live_source.py``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import probe_live_source  # noqa: E402


def test_probe_report_scaffold_matches_current_pbir_metadata_contract() -> None:
    """The reachability probe must not hand report builders a validator-invalid scaffold."""
    files = probe_live_source._pbip_files("Probe", "let\n    Source = #table({}, {})\nin\n    Source", "T", "C")

    definition = json.loads(files["Probe.Report/definition.pbir"])
    version = json.loads(files["Probe.Report/definition/version.json"])
    report = json.loads(files["Probe.Report/definition/report.json"])

    assert definition == {
        "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/"
        "definitionProperties/2.0.0/schema.json",
        "version": "4.0",
        "datasetReference": {"byPath": {"path": "../Probe.SemanticModel"}},
    }
    assert version["version"] == "2.0.0"
    assert "reportVersionAtImport" not in report
