"""Tests for scripts/connection_target.py: what a migrated model must CONNECT TO.

A packaged `.hyper` looks identical whether it caches a CSV or a live warehouse; getting it wrong
yields a model that can never refresh.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
from connection_target import FLAT_FILE, LIVE_SOURCE, UNKNOWN, powerbi_target


@pytest.mark.parametrize("mode", ["live", "extract"])
def test_live_systems_must_be_connected_to_even_when_extracted(mode: str) -> None:
    """`mode == "extract"` must NOT downgrade a live (or unrecognised) source to a file."""
    target, reason = powerbi_target("snowflake", mode)
    assert target == LIVE_SOURCE
    assert "snowflake" in reason
    assert ("cache" in reason and "refresh" in reason) == (mode == "extract")


@pytest.mark.parametrize(
    "connection_class",
    ["excel-direct", "excel", "textscan", "csv", "json", "parquet", "msaccess", "spatial", "ogr", "ogrdirect"],
)
def test_file_sources_are_materialised(connection_class: str) -> None:
    target, reason = powerbi_target(connection_class, "extract")
    assert target == FLAT_FILE
    assert "FILE source" in reason


def test_unknown_class_is_not_guessed() -> None:
    target, reason = powerbi_target("unknown", "live")
    assert target == UNKNOWN
    assert "before building" in reason
