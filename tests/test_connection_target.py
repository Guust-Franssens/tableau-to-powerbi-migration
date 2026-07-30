"""Tests for scripts/connection_target.py — what a migrated model must CONNECT TO.

This is the most consequential mapping decision in a migration and the one Tableau's metadata does
not answer: a packaged `.hyper` extract looks *identical* whether it caches a CSV or a Snowflake
warehouse. Getting it wrong is invisible on day one (the numbers match, because they were copied)
and only surfaces when the customer's first refresh fails or the data is quietly stale.

Reported by a user migrating a live Snowflake workbook.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# ruff: noqa: E402  (the sys.path insert above must precede these imports)
from connection_target import FLAT_FILE, LIVE_SOURCE, UNKNOWN, powerbi_target


@pytest.mark.parametrize(
    "connection_class",
    ["snowflake", "sqlserver", "databricks", "redshift", "bigquery", "postgres", "oracle", "salesforce"],
)
@pytest.mark.parametrize("mode", ["live", "extract"])
def test_live_systems_must_be_connected_to_even_when_extracted(connection_class: str, mode: str) -> None:
    """`mode == "extract"` must NOT downgrade a live source to a file.

    This is the exact case that looks like a flat file and isn't: Tableau caches Snowflake into a
    .hyper, and migrating the cache produces a model that can never refresh.
    """
    target, reason = powerbi_target(connection_class, mode)
    assert target == LIVE_SOURCE
    assert connection_class in reason


def test_live_extract_reason_warns_about_the_cache() -> None:
    """The reason is surfaced to the user, so it must say why the .hyper is not the answer."""
    _, reason = powerbi_target("snowflake", "extract")
    assert "cache" in reason.lower()
    assert "refresh" in reason.lower()


@pytest.mark.parametrize(
    "connection_class",
    ["excel-direct", "excel", "textscan", "csv", "json", "parquet", "msaccess", "spatial"],
)
def test_file_sources_are_materialised(connection_class: str) -> None:
    """For a real file there is no upstream to connect to, so extracting IS the faithful migration."""
    target, _ = powerbi_target(connection_class, "extract")
    assert target == FLAT_FILE


def test_unknown_class_is_not_guessed() -> None:
    """Ask rather than guess - a wrong guess here is silent and expensive."""
    target, reason = powerbi_target("unknown", "live")
    assert target == UNKNOWN
    assert "before building" in reason


def test_unrecognised_class_defaults_to_live_not_file() -> None:
    """Fail SAFE: under-connecting is far worse than over-asking for a credential.

    A class we have never seen is much more likely to be a database/API than a file format, and the
    cost of being wrong is asymmetric - a needless credential prompt versus a silently dead model.
    """
    target, _ = powerbi_target("some-new-cloud-warehouse", "extract")
    assert target == LIVE_SOURCE


def test_parser_stamps_the_target_onto_every_data_source() -> None:
    """The decision has to reach the spec, or the builder cannot act on it."""
    import json

    specs = sorted((REPO_ROOT / "examples").glob("*/migration-spec.json"))
    assert specs, "no example specs found"
    spec = json.loads(specs[0].read_text(encoding="utf-8"))
    for data_source in spec["data_sources"]:
        assert data_source["connection"]["powerbi_target"] in {LIVE_SOURCE, FLAT_FILE, UNKNOWN}
